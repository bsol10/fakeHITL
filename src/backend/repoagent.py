import os
from typing import List, Optional
from pydantic import BaseModel, Field

from llama_index.core.output_parsers import PydanticOutputParser
from llama_index.core.prompts import PromptTemplate
from llama_index.llms.google_genai import GoogleGenAI


class FindingItem(BaseModel):
    title: str = Field(description="Short title of the finding or topic item")
    status: str = Field(description="Status indicator: UPDATED, NEW, or CANNOT VERIFY")
    source: str = Field(description="Source document name or type")
    prior_finding: str = Field(description="Previous finding text or '-- None (New Topic) --'")
    extracted_finding: str = Field(description="Newly extracted or updated finding text")
    source_snippet: str = Field(description="Direct snippet quote from the source text")
    retrieved_info: str = Field(description="Metadata string containing date, location, and confidence")


class ReportData(BaseModel):
    report_name: str = Field(default="Future Skills Report 2026.docx")
    sources_checked: str = Field(default="12 / 12 Sources Checked")
    last_updated: str = Field(default="27 Aug 2026")
    updated_count: int = Field(default=0)
    new_count: int = Field(default=0)
    unverified_count: int = Field(default=0)
    items: List[FindingItem] = Field(default_factory=list)
    draft_section_title: str = Field(default="Section 2: Emerging Skill Deficits")
    draft_summary: str = Field(description="Live preview summary synthesis of approved claims")


def format_ui_layout(data: ReportData) -> str:
    layout = f"""
[ SIDEBAR: CONTROL & CONFIG ]
-----------------------------------------
<Drag & Drop> Selected Report: 
[ {data.report_name} ] 

<Plain text> 
Source Registry Status: {data.sources_checked}

<Button/Plain Text> 
Last Updated: {data.last_updated} 
Workflow Action: [Run Source Update Check] 

<Button> Export Options: 
[Download Approved Draft (.docx)]

--------------------------------------------------------------------------------------------------

[MAIN PANEL]

Status: [ 🟢 Updated ({data.updated_count}) ]  [ 🔵 New ({data.new_count}) ]  [ 🟡 Cannot Verify ({data.unverified_count}) ]
"""

    # Populate items dynamically
    for idx, item in enumerate(data.items, start=1):
        status_icon = "🟢" if "UPDATED" in item.status.upper() else "🔵" if "NEW" in item.status.upper() else "🟡"
        layout += f"""
  ----------------------------------------------------------------------------------------------
  ITEM #{idx}: {item.title}
  ----------------------------------------------------------------------------------------------
  Status:  [ {status_icon} {item.status.upper()} ]                     Source:  {item.source}

  [ PRIOR FINDING ]                            [ EXTRACTED FINDING ]
  "{item.prior_finding}"                      "{item.extracted_finding}"

  Inspect Citation Snippet:
  ┌────────────────────────────────────────────────────────────────────────────────────────────┐
  │ Source Text: "{item.source_snippet}"
  │ Retrieved: {item.retrieved_info}
  └────────────────────────────────────────────────────────────────────────────────────────────┘

  Human Review Action:
  [ Edit Text ]   [ ❌ Reject ]   [ ✅ Approve Finding ]
"""

    # Add draft preview bottom panel
    citations_str = "\n".join([
        f"│ [Source {i + 1}] {item.source} (Verified on {data.last_updated})"
        for i, item in enumerate(data.items)
    ])

    layout += f"""
--------------------------------------------------------------------------------------------------

[ BOTTOM PANEL: LIVE GENERATED DRAFT PREVIEW ]

┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ DRAFT REPORT PREVIEW (Live update based on approved claims only)                               │
│                                                                                                │
│ {data.draft_section_title}                                                             │
│ {data.draft_summary}                                                                          │
│                                                                                                │
│ ── Citations ─────────────────────────────────────────────────────────────────────────────     │
{citations_str}
└────────────────────────────────────────────────────────────────────────────────────────────────┘
"""
    return layout


def parse_message_with_ai(raw_text: str) -> Optional[str]:
    try:
        api_key = os.getenv("[PUT YOUR OWN GEMINI API KEY FOR NOW, WE WILL FIND A SYNTHSISED ONE LATER]")
        llm = GoogleGenAI(model="gemini-3.5-flash", api_key=api_key)

        output_parser = PydanticOutputParser(ReportData)

        prompt_template = PromptTemplate(
            template="""
            You are an AI agent specialized in extracting findings, statistics, and citations from data.
            Analyze the input text and organize findings into structured items.

            Input Data:
            {raw_text}

            {format_instructions}
            """,
            output_parser=output_parser,
        )

        formatted_prompt = prompt_template.format(raw_text=raw_text)
        response = llm.complete(formatted_prompt)

        report_obj: ReportData = output_parser.parse(response.text)

        return format_ui_layout(report_obj)

    except Exception as e:
        print("Error extrapolating from data:", e)
        return None
