//! min-cordis-agent:构建在 min-cordis 之上的 agent 循环参考应用(M3)。
//!
//! 展示框架内核如何组合成经典 agent 周期——感知 → 思考(LLM)→ 行动(工具)→
//! 观察 → …… → 回答:
//!
//! - [`LlmService`]:模型后端边界,作为服务提供(`llm` 键)
//! - [`ToolSpec`]:声明式工具(JSON-schema 参数 + 异步 runner)
//! - [`AgentLoopPlugin`]:循环本身,经插件机制装配,`injects = [llm]`
//!   依赖门控(fiber 在 llm 服务提供前保持 Pending)
//!
//! 契约:工具失败(含未知工具)作为 `error: ...` 结果回喂模型,不崩循环;
//! `max_steps` 约束失控循环;`Agent::stop` 在步间协作取消;
//! 可观测性走事件总线(`AgentStepEvent` / `AgentToolEvent`),
//! 监听器随其 fiber 卸载。serde/serde_json 只出现在本 crate(设计 §三)。

use std::collections::HashMap;
use std::future::Future;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use min_cordis::{BoxFuture, CordisError, Ctx, Effect, Event, Plugin, TypeKey};
use serde_json::{json, Value};

/// llm 服务键(`provide_as`/`get_as`)。
pub fn llm_key() -> TypeKey {
    TypeKey::of::<dyn LlmService>()
}

// ── LLM 边界 ─────────────────────────────────────────────────────

/// 模型后端边界:messages 为运行中对话(OpenAI 形状),tools 为函数 schema 列表。
pub trait LlmService: Send + Sync + 'static {
    fn complete<'a>(
        &'a self,
        messages: &'a [Value],
        tools: &'a [Value],
    ) -> BoxFuture<'a, Result<LlmResponse, String>>;
}

/// 一次模型响应:最终内容、请求的工具调用,或两者。
#[derive(Debug, Clone, Default)]
pub struct LlmResponse {
    pub content: Option<String>,
    pub tool_calls: Vec<ToolCall>,
}

impl LlmResponse {
    pub fn content(s: impl Into<String>) -> Self {
        Self {
            content: Some(s.into()),
            tool_calls: Vec::new(),
        }
    }

    pub fn tool_calls(calls: Vec<ToolCall>) -> Self {
        Self {
            content: None,
            tool_calls: calls,
        }
    }
}

/// 模型请求的一次工具调用。
#[derive(Debug, Clone)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub arguments: Value,
}

impl ToolCall {
    pub fn new(id: impl Into<String>, name: impl Into<String>, arguments: Value) -> Self {
        Self {
            id: id.into(),
            name: name.into(),
            arguments,
        }
    }
}

/// ScriptedLlm 记录的一次调用快照(测试断言模型看到了什么)。
#[derive(Debug, Clone)]
pub struct ScriptedCall {
    pub messages: Vec<Value>,
    pub tools: Vec<Value>,
}

/// 测试用确定性后端:每次弹出一个脚本响应,并记录全部对话。
pub struct ScriptedLlm {
    responses: Mutex<Vec<LlmResponse>>,
    pub calls: Mutex<Vec<ScriptedCall>>,
}

impl ScriptedLlm {
    pub fn new(responses: Vec<LlmResponse>) -> Self {
        Self {
            responses: Mutex::new(responses),
            calls: Mutex::new(Vec::new()),
        }
    }
}

impl LlmService for ScriptedLlm {
    fn complete<'a>(
        &'a self,
        messages: &'a [Value],
        tools: &'a [Value],
    ) -> BoxFuture<'a, Result<LlmResponse, String>> {
        Box::pin(async move {
            self.calls.lock().unwrap().push(ScriptedCall {
                messages: messages.to_vec(),
                tools: tools.to_vec(),
            });
            let mut responses = self.responses.lock().unwrap();
            if responses.is_empty() {
                return Err("ScriptedLLM has no responses left".to_string());
            }
            Ok(responses.remove(0))
        })
    }
}

/// 把任意 [`LlmService`] 作为插件装载(提供 `llm` 服务)。
pub struct LlmPlugin {
    pub name: &'static str,
    pub llm: Arc<dyn LlmService>,
}

impl LlmPlugin {
    pub fn new(llm: Arc<dyn LlmService>) -> Self {
        Self { name: "llm", llm }
    }
}

impl Plugin for LlmPlugin {
    fn name(&self) -> &str {
        self.name
    }

    fn apply<'a>(&'a self, ctx: &'a Ctx) -> BoxFuture<'a, Result<Effect, CordisError>> {
        Box::pin(async move {
            ctx.provide_as(llm_key(), self.llm.clone())?;
            Ok(Effect::Done)
        })
    }
}

// ── 工具 ─────────────────────────────────────────────────────────

/// 声明式工具:JSON-schema 参数 + 异步 runner。
/// runner 收参数对象,返回值字符串化进 tool 消息
///(`Value::String` 原样,其余 JSON 序列化)。
#[derive(Clone)]
pub struct ToolSpec {
    pub name: String,
    pub description: String,
    pub parameters: Value,
    pub run: Arc<dyn Fn(Value) -> BoxFuture<'static, Result<Value, String>> + Send + Sync>,
}

impl ToolSpec {
    pub fn new<F, Fut>(name: &str, description: &str, parameters: Value, run: F) -> Self
    where
        F: Fn(Value) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, String>> + Send + 'static,
    {
        Self {
            name: name.to_string(),
            description: description.to_string(),
            parameters,
            run: Arc::new(move |args: Value| Box::pin(run(args))),
        }
    }
}

// ── 事件(总线可观测性)────────────────────────────────────────────

/// 每步模型响应后发布:步号、内容、工具调用数。
#[derive(Debug, Clone)]
pub struct AgentStepEvent {
    pub step: usize,
    pub content: Option<String>,
    pub tool_calls: usize,
}

impl Event for AgentStepEvent {
    const NAME: &'static str = "agent/step";
    type Value = ();
}

/// 每次工具调用后发布:名称、参数、成败、输出。
#[derive(Debug, Clone)]
pub struct AgentToolEvent {
    pub name: String,
    pub arguments: Value,
    pub ok: bool,
    pub output: String,
}

impl Event for AgentToolEvent {
    const NAME: &'static str = "agent/tool";
    type Value = ();
}

// ── agent 循环 ───────────────────────────────────────────────────

#[derive(Debug, thiserror::Error)]
pub enum AgentError {
    #[error("agent loop stopped between steps")]
    Stopped,
    #[error("agent loop exceeded max_steps={0}")]
    MaxSteps(usize),
    #[error("agent fiber never started: load AgentLoopPlugin via ctx.plugin so inject gating binds the llm")]
    NotStarted,
    #[error("llm failed: {0}")]
    Llm(String),
}

/// agent 服务本体(`get::<Agent>()` 取回)。由 [`AgentLoopPlugin`] 经门控装配。
pub struct Agent {
    llm: Option<Arc<dyn LlmService>>,
    ctx: Ctx,
    tools: HashMap<String, ToolSpec>,
    max_steps: usize,
    steps: AtomicUsize,
    stopped: AtomicBool,
}

impl Agent {
    /// 未经插件装配的惰性实例:`run` 立即返回 [`AgentError::NotStarted`]
    ///(对应 python 的"绕过 ctx.plugin 直接构造"失败快路径)。
    pub fn inert(ctx: Ctx, tools: Vec<ToolSpec>, max_steps: usize) -> Arc<Self> {
        Arc::new(Self {
            llm: None,
            ctx,
            tools: tools.into_iter().map(|t| (t.name.clone(), t)).collect(),
            max_steps,
            steps: AtomicUsize::new(0),
            stopped: AtomicBool::new(false),
        })
    }

    fn new(llm: Arc<dyn LlmService>, ctx: Ctx, tools: Vec<ToolSpec>, max_steps: usize) -> Self {
        Self {
            llm: Some(llm),
            ctx,
            tools: tools.into_iter().map(|t| (t.name.clone(), t)).collect(),
            max_steps,
            steps: AtomicUsize::new(0),
            stopped: AtomicBool::new(false),
        }
    }

    /// 请求协作取消,在步间生效。
    pub fn stop(&self) {
        self.stopped.store(true, Ordering::SeqCst);
    }

    pub fn steps(&self) -> usize {
        self.steps.load(Ordering::SeqCst)
    }

    /// 每步交给模型的函数 schema 列表。
    pub fn tool_schemas(&self) -> Vec<Value> {
        self.tools
            .values()
            .map(|tool| {
                json!({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                })
            })
            .collect()
    }

    /// 对一个用户提问跑完循环,返回最终回答。
    pub async fn run(&self, prompt: &str) -> Result<String, AgentError> {
        let Some(llm) = self.llm.clone() else {
            return Err(AgentError::NotStarted);
        };
        let mut messages: Vec<Value> = vec![json!({ "role": "user", "content": prompt })];
        for _ in 0..self.max_steps {
            if self.stopped.load(Ordering::SeqCst) {
                return Err(AgentError::Stopped);
            }
            self.steps.fetch_add(1, Ordering::SeqCst);
            let schemas = self.tool_schemas();
            let response = llm
                .complete(&messages, &schemas)
                .await
                .map_err(AgentError::Llm)?;
            messages.push(json!({
                "role": "assistant",
                "content": response.content,
                "tool_calls": response
                    .tool_calls
                    .iter()
                    .map(|c| json!({ "id": c.id, "name": c.name, "arguments": c.arguments }))
                    .collect::<Vec<_>>(),
            }));
            self.ctx.events().emit(
                &self.ctx,
                Arc::new(AgentStepEvent {
                    step: self.steps.load(Ordering::SeqCst),
                    content: response.content.clone(),
                    tool_calls: response.tool_calls.len(),
                }),
            );
            if response.tool_calls.is_empty() {
                return Ok(response.content.unwrap_or_default());
            }
            for call in response.tool_calls {
                let (content, _ok) = self.run_tool(&call).await;
                messages.push(json!({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": content,
                }));
            }
        }
        Err(AgentError::MaxSteps(self.max_steps))
    }

    /// 执行一次工具调用;失败转为模型可见的 error 结果,不崩循环。
    /// 工具执行在任务边界:runner 创建或 poll 中的 panic 同样转为
    /// 模型可见错误(评审 #13,对齐 python `except Exception` 的兜底)。
    async fn run_tool(&self, call: &ToolCall) -> (String, bool) {
        let Some(tool) = self.tools.get(&call.name) else {
            let content = format!("error: unknown tool '{}'", call.name);
            self.emit_tool(call, false, &content);
            return (content, false);
        };
        let run = tool.run.clone();
        let arguments = call.arguments.clone();
        let fut =
            match std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || run(arguments))) {
                Ok(fut) => fut,
                Err(p) => {
                    let content = format!("error: {}", panic_message(&p));
                    self.emit_tool(call, false, &content);
                    return (content, false);
                }
            };
        match self.ctx.handle().spawn(fut).await {
            Ok(Ok(value)) => {
                let content = match value {
                    Value::String(s) => s,
                    other => other.to_string(),
                };
                self.emit_tool(call, true, &content);
                (content, true)
            }
            Ok(Err(e)) => {
                let content = format!("error: {e}");
                self.emit_tool(call, false, &content);
                (content, false)
            }
            Err(join_err) => {
                // 取消不是 panic:into_panic 在取消场景会二次 panic(评审 P2)
                let content = if join_err.is_panic() {
                    format!("error: {}", panic_message(&join_err.into_panic()))
                } else {
                    "error: tool task cancelled".to_string()
                };
                self.emit_tool(call, false, &content);
                (content, false)
            }
        }
    }

    fn emit_tool(&self, call: &ToolCall, ok: bool, output: &str) {
        self.ctx.events().emit(
            &self.ctx,
            Arc::new(AgentToolEvent {
                name: call.name.clone(),
                arguments: call.arguments.clone(),
                ok,
                output: output.to_string(),
            }),
        );
    }
}

/// 任务边界捕获的 panic 转消息(与核心 panic_error 同构,crate 私有)。
fn panic_message(p: &Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = p.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = p.downcast_ref::<String>() {
        s.clone()
    } else {
        "tool panicked".to_string()
    }
}

/// agent 循环插件:`injects = [llm]` 依赖门控,就绪后提供 `Agent` 服务;
/// 并把 fiber 取消 token 接到 `Agent::stop`(D7 协作停止示范)。
pub struct AgentLoopPlugin {
    tools: Vec<ToolSpec>,
    max_steps: usize,
    inject_keys: Vec<TypeKey>,
}

impl AgentLoopPlugin {
    pub fn new(tools: Vec<ToolSpec>) -> Self {
        Self::with_max_steps(tools, 16)
    }

    pub fn with_max_steps(tools: Vec<ToolSpec>, max_steps: usize) -> Self {
        Self {
            tools,
            max_steps,
            inject_keys: vec![llm_key()],
        }
    }
}

impl Plugin for AgentLoopPlugin {
    fn name(&self) -> &str {
        "agent-loop"
    }

    fn injects(&self) -> &[TypeKey] {
        &self.inject_keys
    }

    fn apply<'a>(&'a self, ctx: &'a Ctx) -> BoxFuture<'a, Result<Effect, CordisError>> {
        Box::pin(async move {
            // 门控保证此处 llm 必在(python `_init` 绑定的对应物)
            let llm = ctx
                .get_as::<dyn LlmService>(llm_key())
                .ok_or_else(|| CordisError::InjectUnsatisfied(vec!["llm".to_string()]))?;
            let agent = Arc::new(Agent::new(
                llm,
                ctx.clone(),
                self.tools.clone(),
                self.max_steps,
            ));
            ctx.provide_as::<Agent>(TypeKey::of::<Agent>(), agent.clone())?;

            // ctx.cancelled → Agent::stop:fiber 卸载级联停止循环
            let watcher_ctx = ctx.clone();
            ctx.effect(move || {
                let watcher_ctx = watcher_ctx.clone();
                let agent = agent.clone();
                watcher_ctx.handle().clone().spawn(async move {
                    watcher_ctx.cancelled().await;
                    agent.stop();
                });
                Effect::Done
            })?;
            Ok(Effect::Done)
        })
    }
}
