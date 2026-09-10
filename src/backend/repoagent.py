import os
from typing import Optional
from pydantic import BaseModel

from llama_index.core.output_parsers import PydanticOutputParser
from llama_index.core.prompts import PromptTemplate
from llama_index.llms.google_genai import GoogleGenAI

class PromptData(BaseModel):
    ai_notes: Optional[str] = None

os.environ["GOOGLE_API_KEY"] = "[PUT YOUR OWN GEMINI API KEY FOR NOW, WE WILL FIND A SYNTHSISED ONE LATER]"

def parse_message_with_ai(raw_text: str) -> Optional[PromptData]:
    try:
        llm = GoogleGenAI(model="gemini-3.6-flash")

        output_parser = PydanticOutputParser(PromptData)

        prompt_template = PromptTemplate(
            template="""
            You are an AI agent specialized in extrapolating data from Tables and Infographics.
            Extract and create your own conclusions all relevant from the following text:

            {raw_text}

            {format_instructions}
            """,
            output_parser=output_parser,
        )

        formatted_prompt = prompt_template.format(raw_text=raw_text)

        response = llm.complete(formatted_prompt)
        findings_data: PromptData = output_parser.parse(response.text)

        return findings_data

    except Exception as e:
        print("Error extrapolating from data:", e)
        return None
