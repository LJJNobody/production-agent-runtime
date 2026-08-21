"""Simple, ReAct, Reflection, and Plan-and-Solve Agent patterns."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Protocol

from agent_runtime.models import AgentKind, LLMRequest, LLMResponse, Message

CompleteFunction = Callable[[LLMRequest, str], Awaitable[LLMResponse]]
ToolFunction = Callable[[str, Dict[str, object], str], Awaitable[str]]


@dataclass
class AgentContext:
    task: str
    history: List[Message]
    max_steps: int
    tool_descriptions: List[Dict[str, str]]
    complete: CompleteFunction
    execute_tool: ToolFunction


class AgentPattern(Protocol):
    async def run(self, context: AgentContext) -> str:
        ...


class SimpleAgent:
    async def run(self, context: AgentContext) -> str:
        messages = [
            Message(
                "system",
                "You are a reliable assistant. Answer the current request directly and clearly.",
            ),
            *context.history,
            Message("user", context.task),
        ]
        return (await context.complete(LLMRequest(messages), "simple.generate")).content


class ReActAgent:
    async def run(self, context: AgentContext) -> str:
        tool_text = json.dumps(context.tool_descriptions, ensure_ascii=False)
        messages = [
            Message(
                "system",
                "You are a ReAct controller. At every turn return one JSON object only. "
                'Use {"thought":"...","action":"tool","action_input":{...}} to call a tool, '
                'or {"thought":"...","final":"..."} to finish. Never invent tool output. '
                f"Available tools: {tool_text}",
            ),
            *context.history,
            Message("user", context.task),
        ]
        for index in range(context.max_steps):
            response = await context.complete(
                LLMRequest(messages),
                f"react.reason.{index + 1}",
            )
            decision = _parse_object(response.content)
            final = decision.get("final")
            if isinstance(final, str) and final.strip():
                return final.strip()
            action = decision.get("action")
            arguments = decision.get("action_input", {})
            if not isinstance(action, str) or not action.strip():
                return response.content
            if not isinstance(arguments, dict):
                arguments = {}
            observation = await context.execute_tool(
                action,
                arguments,
                f"react.tool.{index + 1}",
            )
            messages.extend(
                [
                    Message("assistant", response.content),
                    Message(
                        "user",
                        f"Tool observation for {action}:\n{observation}\nContinue with one JSON object.",
                    ),
                ]
            )
        messages.append(
            Message("user", 'Stop using tools and return {"thought":"...","final":"..."}.')
        )
        response = await context.complete(LLMRequest(messages), "react.final")
        decision = _parse_object(response.content)
        final = decision.get("final")
        return final.strip() if isinstance(final, str) and final.strip() else response.content


class ReflectionAgent:
    async def run(self, context: AgentContext) -> str:
        base = [*context.history, Message("user", context.task)]
        draft = await context.complete(
            LLMRequest(
                [
                    Message("system", "Produce a factually grounded first draft."),
                    *base,
                ]
            ),
            "reflection.draft",
        )
        critique = await context.complete(
            LLMRequest(
                [
                    Message(
                        "system",
                        "Critique the draft. Identify factual gaps, missing constraints, and unclear reasoning.",
                    ),
                    Message("user", f"Task:\n{context.task}\n\nDraft:\n{draft.content}"),
                ]
            ),
            "reflection.critique",
        )
        revised = await context.complete(
            LLMRequest(
                [
                    Message(
                        "system",
                        "Revise the draft using the critique. Return only the improved final answer.",
                    ),
                    Message(
                        "user",
                        f"Task:\n{context.task}\n\nDraft:\n{draft.content}\n\nCritique:\n{critique.content}",
                    ),
                ]
            ),
            "reflection.revise",
        )
        return revised.content


class PlanSolveAgent:
    async def run(self, context: AgentContext) -> str:
        plan_response = await context.complete(
            LLMRequest(
                [
                    Message(
                        "system",
                        "Create a concise execution plan as a JSON array of strings only.",
                    ),
                    *context.history,
                    Message("user", context.task),
                ]
            ),
            "plan_solve.plan",
        )
        plan = _parse_plan(plan_response.content)[: context.max_steps]
        results: List[str] = []
        for index, step in enumerate(plan, start=1):
            prior = "\n".join(results)
            response = await context.complete(
                LLMRequest(
                    [
                        Message(
                            "system",
                            "Solve only the assigned plan step using the task and prior results.",
                        ),
                        Message(
                            "user",
                            f"Task: {context.task}\nStep {index}: {step}\nPrior results:\n{prior}",
                        ),
                    ]
                ),
                f"plan_solve.step.{index}",
            )
            results.append(f"Step {index} ({step}): {response.content}")
        synthesis = await context.complete(
            LLMRequest(
                [
                    Message(
                        "system",
                        "Synthesize the step results into one direct final answer. Do not mention the plan.",
                    ),
                    Message(
                        "user",
                        f"Task: {context.task}\n\nStep results:\n" + "\n".join(results),
                    ),
                ]
            ),
            "plan_solve.synthesize",
        )
        return synthesis.content


def create_patterns() -> Dict[AgentKind, AgentPattern]:
    return {
        AgentKind.SIMPLE: SimpleAgent(),
        AgentKind.REACT: ReActAgent(),
        AgentKind.REFLECTION: ReflectionAgent(),
        AgentKind.PLAN_SOLVE: PlanSolveAgent(),
    }


def _parse_object(value: str) -> Dict[str, object]:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1]).strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"final": value}
    return parsed if isinstance(parsed, dict) else {"final": value}


def _parse_plan(value: str) -> List[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = [line.strip(" -\t") for line in value.splitlines() if line.strip()]
    if not isinstance(parsed, list):
        return [value.strip()]
    plan = [str(item).strip() for item in parsed if str(item).strip()]
    return plan or ["Answer the task"]
