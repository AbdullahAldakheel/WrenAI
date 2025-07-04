import logging
import sys
from typing import Any, Optional

from hamilton import base
from hamilton.async_driver import AsyncDriver
from haystack.components.builders.prompt_builder import PromptBuilder
from langfuse.decorators import observe

from src.core.engine import Engine
from src.core.pipeline import BasicPipeline
from src.core.provider import LLMProvider
from src.pipelines.generation.utils.sql import (
    SQL_GENERATION_MODEL_KWARGS,
    SQLGenPostProcessor,
    calculated_field_instructions,
    construct_ask_history_messages,
    construct_instructions,
    metric_instructions,
    sql_generation_system_prompt,
)
from src.pipelines.retrieval.sql_functions import SqlFunction
from src.utils import trace_cost
from src.web.v1.services import Configuration
from src.web.v1.services.ask import AskHistory

logger = logging.getLogger("wren-ai-service")


text_to_sql_with_followup_user_prompt_template = """
### TASK ###
Given the following user's follow-up question and previous SQL query and summary,
generate one SQL query to best answer user's question.

### DATABASE SCHEMA ###
{% for document in documents %}
    {{ document }}
{% endfor %}

{% if calculated_field_instructions %}
{{ calculated_field_instructions }}
{% endif %}

{% if metric_instructions %}
{{ metric_instructions }}
{% endif %}

{% if sql_functions %}
### SQL FUNCTIONS ###
{% for function in sql_functions %}
{{ function }}
{% endfor %}
{% endif %}

{% if sql_samples %}
### SQL SAMPLES ###
{% for sample in sql_samples %}
Summary:
{{sample.summary}}
SQL:
{{sample.sql}}
{% endfor %}
{% endif %}

{% if instructions %}
### USER INSTRUCTIONS ###
{% for instruction in instructions %}
{{ loop.index }}. {{ instruction }}
{% endfor %}
{% endif %}

### QUESTION ###
User's Follow-up Question: {{ query }}

### REASONING PLAN ###
{{ sql_generation_reasoning }}

Let's think step by step.
"""


## Start of Pipeline
@observe(capture_input=False)
def prompt(
    query: str,
    documents: list[str],
    sql_generation_reasoning: str,
    configuration: Configuration,
    prompt_builder: PromptBuilder,
    sql_samples: list[dict] | None = None,
    instructions: list[dict] | None = None,
    has_calculated_field: bool = False,
    has_metric: bool = False,
    sql_functions: list[SqlFunction] | None = None,
) -> dict:
    return prompt_builder.run(
        query=query,
        documents=documents,
        sql_generation_reasoning=sql_generation_reasoning,
        instructions=construct_instructions(
            instructions=instructions,
        ),
        calculated_field_instructions=calculated_field_instructions
        if has_calculated_field
        else "",
        metric_instructions=metric_instructions if has_metric else "",
        sql_samples=sql_samples,
        sql_functions=sql_functions,
    )


@observe(as_type="generation", capture_input=False)
@trace_cost
async def generate_sql_in_followup(
    prompt: dict, generator: Any, histories: list[AskHistory], generator_name: str
) -> dict:
    history_messages = construct_ask_history_messages(histories)
    return await generator(
        prompt=prompt.get("prompt"), history_messages=history_messages
    ), generator_name


@observe(capture_input=False)
async def post_process(
    generate_sql_in_followup: dict,
    post_processor: SQLGenPostProcessor,
    engine_timeout: float,
    project_id: str | None = None,
) -> dict:
    return await post_processor.run(
        generate_sql_in_followup.get("replies"),
        timeout=engine_timeout,
        project_id=project_id,
    )


## End of Pipeline


class FollowUpSQLGeneration(BasicPipeline):
    def __init__(
        self,
        llm_provider: LLMProvider,
        engine: Engine,
        engine_timeout: Optional[float] = 30.0,
        **kwargs,
    ):
        self._components = {
            "generator": llm_provider.get_generator(
                system_prompt=sql_generation_system_prompt,
                generation_kwargs=SQL_GENERATION_MODEL_KWARGS,
            ),
            "generator_name": llm_provider.get_model(),
            "prompt_builder": PromptBuilder(
                template=text_to_sql_with_followup_user_prompt_template
            ),
            "post_processor": SQLGenPostProcessor(engine=engine),
        }

        self._configs = {
            "engine_timeout": engine_timeout,
        }

        super().__init__(
            AsyncDriver({}, sys.modules[__name__], result_builder=base.DictResult())
        )

    @observe(name="Follow-Up SQL Generation")
    async def run(
        self,
        query: str,
        contexts: list[str],
        sql_generation_reasoning: str,
        histories: list[AskHistory],
        configuration: Configuration = Configuration(),
        sql_samples: list[dict] | None = None,
        instructions: list[dict] | None = None,
        project_id: str | None = None,
        has_calculated_field: bool = False,
        has_metric: bool = False,
        sql_functions: list[SqlFunction] | None = None,
    ):
        logger.info("Follow-Up SQL Generation pipeline is running...")
        return await self._pipe.execute(
            ["post_process"],
            inputs={
                "query": query,
                "documents": contexts,
                "sql_generation_reasoning": sql_generation_reasoning,
                "histories": histories,
                "project_id": project_id,
                "configuration": configuration,
                "sql_samples": sql_samples,
                "instructions": instructions,
                "has_calculated_field": has_calculated_field,
                "has_metric": has_metric,
                "sql_functions": sql_functions,
                **self._components,
                **self._configs,
            },
        )


if __name__ == "__main__":
    from src.pipelines.common import dry_run_pipeline

    dry_run_pipeline(
        FollowUpSQLGeneration,
        "followup_sql_generation",
        query="show me the dataset",
        contexts=[],
        history=AskHistory(sql="SELECT * FROM table", summary="Summary", steps=[]),
    )
