//! 可运行 demo(M3):weather 工具 + ScriptedLLM + agent 循环。
//!
//! ```text
//! cargo run -p min-cordis-agent --example demo
//! ```

use std::sync::Arc;

use min_cordis::Ctx;
use min_cordis_agent::{
    Agent, AgentLoopPlugin, LlmPlugin, LlmResponse, ScriptedLlm, ToolCall, ToolSpec,
};
use serde_json::json;

#[tokio::main]
async fn main() {
    let root = Ctx::root().expect("run inside a tokio runtime");

    let weather = ToolSpec::new(
        "get_weather",
        "current weather for a city",
        json!({ "type": "object", "properties": { "city": { "type": "string" } } }),
        |args| async move {
            let city = args["city"].as_str().unwrap_or("?").to_string();
            // 真实实现里这里会请求天气 API
            Ok(json!({ "city": city, "temp": 18, "sky": "clear" }))
        },
    );

    // 脚本后端:先要一次工具,再给最终回答
    let llm = Arc::new(ScriptedLlm::new(vec![
        LlmResponse::tool_calls(vec![ToolCall::new(
            "1",
            "get_weather",
            json!({ "city": "Oslo" }),
        )]),
        LlmResponse::content("Oslo: 18 degrees, clear sky."),
    ]));

    let llm_view = root.plugin(LlmPlugin::new(llm));
    let agent_view = root.plugin(AgentLoopPlugin::new(vec![weather]));
    (&llm_view).await.expect("llm loads");
    (&agent_view).await.expect("agent loads (gated on llm)");

    let agent = root.get::<Agent>().unwrap();
    match agent.run("weather in Oslo?").await {
        Ok(answer) => println!("answer: {answer}"),
        Err(e) => eprintln!("agent failed: {e}"),
    }
    println!("steps: {}", agent.steps());

    agent_view.dispose().await.unwrap();
    llm_view.dispose().await.unwrap();
    println!("unloaded cleanly");
}
