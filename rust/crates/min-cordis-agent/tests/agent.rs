//! agent 组契约测试(§五):python examples 11 项范式语义的 Rust 对应物。

use std::sync::{Arc, Mutex};
use std::time::Duration;

use min_cordis::{Ctx, FiberState, FiberView, Listener, Plugin};
use min_cordis_agent::{
    llm_key, Agent, AgentError, AgentLoopPlugin, AgentStepEvent, AgentToolEvent, LlmPlugin,
    LlmResponse, ScriptedLlm, ToolCall, ToolSpec,
};
use serde_json::{json, Value};

fn weather_tool() -> ToolSpec {
    ToolSpec::new(
        "get_weather",
        "current weather for a city",
        json!({ "type": "object", "properties": { "city": { "type": "string" } } }),
        |args: Value| async move {
            let city = args["city"].as_str().unwrap_or("?").to_string();
            Ok(json!({ "city": city, "temp": 30 }))
        },
    )
}

/// 组装 llm + agent(都走插件机制),返回 (agent_view, llm_view, llm)。
async fn load_agent(
    root: &Ctx,
    responses: Vec<LlmResponse>,
    tools: Vec<ToolSpec>,
    max_steps: Option<usize>,
) -> (FiberView, FiberView, Arc<ScriptedLlm>) {
    let llm = Arc::new(ScriptedLlm::new(responses));
    let llm_view = root.plugin(LlmPlugin::new(llm.clone()));
    let plugin = match max_steps {
        Some(m) => AgentLoopPlugin::with_max_steps(tools, m),
        None => AgentLoopPlugin::new(tools),
    };
    let agent_view = root.plugin(plugin);
    (&llm_view).await.expect("llm loads");
    (&agent_view).await.expect("agent loads");
    (agent_view, llm_view, llm)
}

fn tool_messages(llm: &ScriptedLlm, call_index: usize) -> Vec<Value> {
    llm.calls.lock().unwrap()[call_index]
        .messages
        .iter()
        .filter(|m| m["role"] == "tool")
        .cloned()
        .collect()
}

async fn soon<F, T>(f: F) -> T
where
    F: std::future::Future<Output = T>,
{
    tokio::time::timeout(Duration::from_secs(5), f)
        .await
        .expect("timed out")
}

// 1. 直接回答,无工具
#[tokio::test]
async fn test_direct_answer_without_tools() {
    let root = Ctx::root().unwrap();
    let (_av, _lv, llm) = load_agent(&root, vec![LlmResponse::content("42")], vec![], None).await;

    assert_eq!(
        root.get::<Agent>()
            .unwrap()
            .run("meaning of life?")
            .await
            .unwrap(),
        "42"
    );

    let calls = llm.calls.lock().unwrap();
    assert_eq!(
        calls[0].messages,
        vec![json!({ "role": "user", "content": "meaning of life?" })]
    );
    assert!(calls[0].tools.is_empty());
    assert_eq!(root.get::<Agent>().unwrap().steps(), 1);
}

// 2. 工具往返:第二次模型调用看到 assistant 工具调用与工具结果
#[tokio::test]
async fn test_tool_round_trip() {
    let root = Ctx::root().unwrap();
    let (_av, _lv, llm) = load_agent(
        &root,
        vec![
            LlmResponse::tool_calls(vec![ToolCall::new(
                "1",
                "get_weather",
                json!({ "city": "Oslo" }),
            )]),
            LlmResponse::content("Oslo: 30 degrees"),
        ],
        vec![weather_tool()],
        None,
    )
    .await;

    assert_eq!(
        root.get::<Agent>()
            .unwrap()
            .run("weather in Oslo?")
            .await
            .unwrap(),
        "Oslo: 30 degrees"
    );

    let fed = tool_messages(&llm, 1);
    assert_eq!(fed.len(), 1);
    assert_eq!(fed[0]["tool_call_id"], "1");
    assert_eq!(fed[0]["name"], "get_weather");
    assert_eq!(
        serde_json::from_str::<Value>(fed[0]["content"].as_str().unwrap()).unwrap(),
        json!({ "city": "Oslo", "temp": 30 })
    );
    // schema 每次调用都提供
    let calls = llm.calls.lock().unwrap();
    assert_eq!(calls[1].tools[0]["function"]["name"], "get_weather");
}

// 3. 多工具调用按序执行,结果按序回喂
#[tokio::test]
async fn test_multiple_tool_calls_run_in_order() {
    let root = Ctx::root().unwrap();
    let order: Arc<Mutex<Vec<(i64, i64)>>> = Arc::new(Mutex::new(Vec::new()));
    let o = order.clone();
    let add_tool = ToolSpec::new(
        "add",
        "sum two integers",
        json!({ "type": "object", "properties": { "a": { "type": "integer" }, "b": { "type": "integer" } } }),
        move |args: Value| {
            let o = o.clone();
            async move {
                let a = args["a"].as_i64().unwrap();
                let b = args["b"].as_i64().unwrap();
                o.lock().unwrap().push((a, b));
                Ok(Value::String((a + b).to_string()))
            }
        },
    );
    let (_av, _lv, llm) = load_agent(
        &root,
        vec![
            LlmResponse::tool_calls(vec![
                ToolCall::new("t1", "add", json!({ "a": 1, "b": 2 })),
                ToolCall::new("t2", "add", json!({ "a": 3, "b": 4 })),
            ]),
            LlmResponse::content("done"),
        ],
        vec![add_tool],
        None,
    )
    .await;

    assert_eq!(
        root.get::<Agent>().unwrap().run("add twice").await.unwrap(),
        "done"
    );
    assert_eq!(*order.lock().unwrap(), vec![(1, 2), (3, 4)]);
    let fed = tool_messages(&llm, 1);
    assert_eq!(
        fed.iter()
            .map(|m| m["tool_call_id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["t1", "t2"]
    );
    assert_eq!(
        fed.iter()
            .map(|m| m["content"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["3", "7"]
    );
}

// 4. 工具失败回喂模型,循环不崩
#[tokio::test]
async fn test_tool_error_is_fed_back_not_fatal() {
    let root = Ctx::root().unwrap();
    let bad = ToolSpec::new(
        "bad",
        "always fails",
        json!({ "type": "object", "properties": { "x": { "type": "integer" } } }),
        |_args: Value| async { Err::<Value, String>("boom".to_string()) },
    );
    let (_av, _lv, llm) = load_agent(
        &root,
        vec![
            LlmResponse::tool_calls(vec![ToolCall::new("e1", "bad", json!({ "x": 1 }))]),
            LlmResponse::content("recovered"),
        ],
        vec![bad],
        None,
    )
    .await;

    assert_eq!(
        root.get::<Agent>().unwrap().run("try it").await.unwrap(),
        "recovered"
    );
    let fed = tool_messages(&llm, 1);
    assert_eq!(fed[0]["content"], "error: boom");
}

// 5. 未知工具报告给模型
#[tokio::test]
async fn test_unknown_tool_reported_to_model() {
    let root = Ctx::root().unwrap();
    let (_av, _lv, llm) = load_agent(
        &root,
        vec![
            LlmResponse::tool_calls(vec![ToolCall::new("u1", "nope", json!({}))]),
            LlmResponse::content("ok"),
        ],
        vec![],
        None,
    )
    .await;

    assert_eq!(
        root.get::<Agent>()
            .unwrap()
            .run("call something odd")
            .await
            .unwrap(),
        "ok"
    );
    let fed = tool_messages(&llm, 1);
    assert!(fed[0]["content"].as_str().unwrap().contains("unknown tool"));
}

// 6. 异步工具
#[tokio::test]
async fn test_async_tool_supported() {
    let root = Ctx::root().unwrap();
    let tool = ToolSpec::new(
        "double",
        "async doubling",
        json!({ "type": "object", "properties": { "x": { "type": "integer" } } }),
        |args: Value| async move {
            tokio::time::sleep(Duration::from_millis(10)).await;
            Ok(Value::String((args["x"].as_i64().unwrap() * 2).to_string()))
        },
    );
    let (_av, _lv, llm) = load_agent(
        &root,
        vec![
            LlmResponse::tool_calls(vec![ToolCall::new("d1", "double", json!({ "x": 4 }))]),
            LlmResponse::content("8 it is"),
        ],
        vec![tool],
        None,
    )
    .await;

    assert_eq!(
        root.get::<Agent>().unwrap().run("double 4").await.unwrap(),
        "8 it is"
    );
    assert_eq!(tool_messages(&llm, 1)[0]["content"], "8");
}

// 7. max_steps 约束失控循环
#[tokio::test]
async fn test_max_steps_bounds_the_loop() {
    let root = Ctx::root().unwrap();
    let endless: Vec<LlmResponse> = (0..10)
        .map(|i| {
            LlmResponse::tool_calls(vec![ToolCall::new(
                format!("c{i}"),
                "get_weather",
                json!({ "city": "x" }),
            )])
        })
        .collect();
    let (_av, _lv, llm) = load_agent(&root, endless, vec![weather_tool()], Some(3)).await;

    let err = root
        .get::<Agent>()
        .unwrap()
        .run("run forever")
        .await
        .unwrap_err();
    assert!(matches!(err, AgentError::MaxSteps(3)));
    assert_eq!(llm.calls.lock().unwrap().len(), 3);
}

// 8. stop 在步间协作取消
#[tokio::test]
async fn test_stop_cancels_between_steps() {
    let root = Ctx::root().unwrap();
    let root2 = root.clone();
    let stop_tool = ToolSpec::new(
        "stop",
        "request cancellation",
        json!({ "type": "object", "properties": {} }),
        move |_args: Value| {
            let root2 = root2.clone();
            async move {
                root2.get::<Agent>().unwrap().stop();
                Ok(Value::String("stopping".to_string()))
            }
        },
    );
    let (_av, _lv, llm) = load_agent(
        &root,
        vec![
            LlmResponse::tool_calls(vec![ToolCall::new("s1", "stop", json!({}))]),
            LlmResponse::content("never reached"),
        ],
        vec![stop_tool],
        None,
    )
    .await;

    let err = root.get::<Agent>().unwrap().run("go").await.unwrap_err();
    assert!(matches!(err, AgentError::Stopped));
    // 第一轮工具后、下一次模型调用前停止
    assert_eq!(llm.calls.lock().unwrap().len(), 1);
}

// 9. inject 门控:agent 先装载,llm 后到
#[tokio::test]
async fn test_inject_gating_waits_for_llm() {
    let root = Ctx::root().unwrap();
    let agent_view = root.plugin(AgentLoopPlugin::new(vec![weather_tool()]));
    tokio::time::sleep(Duration::from_millis(50)).await;
    // llm 未提供:agent 服务不存在(严格读取)
    assert!(root
        .get_as::<dyn min_cordis_agent::LlmService>(llm_key())
        .is_none());
    assert!(root.get::<Agent>().is_none());
    assert_eq!(agent_view.state().state, FiberState::Pending);

    let llm = Arc::new(ScriptedLlm::new(vec![LlmResponse::content("late")]));
    let llm_view = root.plugin(LlmPlugin::new(llm));
    (&llm_view).await.expect("llm loads");
    (&agent_view).await.expect("gating passed and agent bound");

    assert_eq!(
        root.get::<Agent>().unwrap().run("hi").await.unwrap(),
        "late"
    );
}

// 10. 绕过插件装配直接构造:run 立即失败
#[tokio::test]
async fn test_run_without_plugin_start_fails_fast() {
    let root = Ctx::root().unwrap();
    let agent = Agent::inert(root.clone(), vec![weather_tool()], 16);
    let err = soon(agent.run("hi")).await.unwrap_err();
    assert!(matches!(err, AgentError::NotStarted));
    assert!(err.to_string().contains("ctx.plugin"));
}

// 11. 事件流经总线;监听器随其 fiber 卸载;fiber 对卸载后可整体重载
#[tokio::test]
async fn test_events_flow_and_unload_with_fiber() {
    let root = Ctx::root().unwrap();

    // 审计插件:在自己的 fiber 上挂两个监听器(D28:随 fiber 卸载)
    #[derive(Clone)]
    enum Seen {
        Step(usize, Option<String>, usize),
        Tool(String, bool),
    }
    let events: Arc<Mutex<Vec<Seen>>> = Arc::new(Mutex::new(Vec::new()));

    struct StepL(Arc<Mutex<Vec<Seen>>>);
    impl Listener<AgentStepEvent> for StepL {
        fn call<'a>(
            &'a self,
            _c: &'a Ctx,
            e: &'a AgentStepEvent,
        ) -> min_cordis::BoxFuture<'a, Result<Option<()>, min_cordis::CordisError>> {
            let ev = self.0.clone();
            let e = e.clone();
            Box::pin(async move {
                ev.lock()
                    .unwrap()
                    .push(Seen::Step(e.step, e.content.clone(), e.tool_calls));
                Ok(None)
            })
        }
    }
    struct ToolL(Arc<Mutex<Vec<Seen>>>);
    impl Listener<AgentToolEvent> for ToolL {
        fn call<'a>(
            &'a self,
            _c: &'a Ctx,
            e: &'a AgentToolEvent,
        ) -> min_cordis::BoxFuture<'a, Result<Option<()>, min_cordis::CordisError>> {
            let ev = self.0.clone();
            let e = e.clone();
            Box::pin(async move {
                ev.lock().unwrap().push(Seen::Tool(e.name.clone(), e.ok));
                Ok(None)
            })
        }
    }

    struct Audit {
        events: Arc<Mutex<Vec<Seen>>>,
    }
    impl Plugin for Audit {
        fn name(&self) -> &str {
            "audit"
        }
        fn apply<'a>(
            &'a self,
            ctx: &'a Ctx,
        ) -> min_cordis::BoxFuture<'a, Result<min_cordis::Effect, min_cordis::CordisError>>
        {
            let events = self.events.clone();
            Box::pin(async move {
                ctx.events().on(ctx, StepL(events.clone()))?;
                ctx.events().on(ctx, ToolL(events))?;
                Ok(min_cordis::Effect::Done)
            })
        }
    }
    let audit_view = root.plugin(Audit {
        events: events.clone(),
    });
    (&audit_view).await.expect("audit loads");

    let (agent_view, llm_view, _llm) = load_agent(
        &root,
        vec![
            LlmResponse::tool_calls(vec![ToolCall::new(
                "1",
                "get_weather",
                json!({ "city": "Oslo" }),
            )]),
            LlmResponse::content("30"),
        ],
        vec![weather_tool()],
        None,
    )
    .await;
    assert_eq!(root.get::<Agent>().unwrap().run("w").await.unwrap(), "30");

    soon(async {
        while events.lock().unwrap().len() < 3 {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await;
    let snapshot = events.lock().unwrap().clone();
    // emit 为 fire-and-forget,监听器任务的完成顺序无保证(评审 #15):
    // 断言按内容与步序,不断言到达顺序
    let steps: Vec<_> = snapshot
        .iter()
        .filter(|e| matches!(e, Seen::Step(..)))
        .cloned()
        .collect();
    let tools: Vec<_> = snapshot
        .iter()
        .filter(|e| matches!(e, Seen::Tool(..)))
        .cloned()
        .collect();
    assert_eq!(steps.len(), 2);
    assert!(matches!(&steps[0], Seen::Step(1, None, 1)));
    assert!(matches!(&steps[1], Seen::Step(2, Some(c), 0) if c == "30"));
    assert_eq!(tools.len(), 1);
    assert!(matches!(&tools[0], Seen::Tool(name, true) if name == "get_weather"));

    // 监听器随 fiber 卸载;处置后的 agent/llm 对可在同一 root 重新装载
    agent_view.dispose().await.unwrap();
    llm_view.dispose().await.unwrap();
    audit_view.dispose().await.unwrap();
    assert!(root.get::<Agent>().is_none());

    let (_av2, _lv2, _llm2) =
        load_agent(&root, vec![LlmResponse::content("again")], vec![], None).await;
    assert_eq!(
        root.get::<Agent>().unwrap().run("again").await.unwrap(),
        "again"
    );
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(events.lock().unwrap().len(), 3); // 无残留监听器
}

// #13(评审):工具 runner panic 转为模型可见错误,循环不崩
#[tokio::test]
async fn test_tool_panic_is_fed_back() {
    let root = Ctx::root().unwrap();
    let tool = ToolSpec::new(
        "boom",
        "panics on poll",
        json!({ "type": "object", "properties": {} }),
        |_args: Value| async {
            if true {
                panic!("tool boom");
            }
            #[allow(unreachable_code)]
            Ok::<Value, String>(Value::Null)
        },
    );
    let (_av, _lv, llm) = load_agent(
        &root,
        vec![
            LlmResponse::tool_calls(vec![ToolCall::new("p1", "boom", json!({}))]),
            LlmResponse::content("recovered2"),
        ],
        vec![tool],
        None,
    )
    .await;

    assert_eq!(
        root.get::<Agent>().unwrap().run("go").await.unwrap(),
        "recovered2"
    );
    let fed = tool_messages(&llm, 1);
    assert!(fed[0]["content"].as_str().unwrap().starts_with("error:"));
}

// 连续运行(未 stop):第二次 run 正常(python 语义:stop 才粘滞)
#[tokio::test]
async fn test_sequential_runs_without_stop() {
    let root = Ctx::root().unwrap();
    let (_av, _lv, _llm) = load_agent(
        &root,
        vec![
            LlmResponse::content("first"),
            LlmResponse::content("second"),
        ],
        vec![],
        None,
    )
    .await;

    let agent = root.get::<Agent>().unwrap();
    assert_eq!(agent.run("q1").await.unwrap(), "first");
    assert_eq!(agent.run("q2").await.unwrap(), "second");
    assert_eq!(agent.steps(), 2); // 累计步数(python 同语义)
}
