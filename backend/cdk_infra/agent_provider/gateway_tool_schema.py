#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""
AgentCore Gateway Tool Schema
==============================

Defines the MCP tool schemas for the Lambda target.
Imported by the CDK stack when configuring the Gateway target.
"""

# Reserved. The gateway's REQUEST interceptor overwrites this argument with the caller's
# verified subject on every tool call; each tool declares it because the gateway forwards
# only the arguments a tool's inputSchema names. The same name as gateway_event.py's
# SUBJECT_ARGUMENT, which the tool functions read (tests/unit/test_identity_isolation.py).
SUBJECT_ARGUMENT = "_verified_user_id"
SUBJECT_PROPERTY = {
    "type": "string",
    "description": "Reserved for internal use. Do not set this. Any value you provide is discarded and replaced.",
}

# The data source tools, served by the dispatcher (mcp_handler.py).
DATA_SOURCE_TOOLS = [
    {
        "name": "search_schema",
        "description": (
            "Search the schemas of your connected data sources to discover the "
            "objects (tables, views) relevant to your query. Returns the definition "
            "of each (CREATE TABLE DDL, for a database), tagged with the data source "
            "and engine it came from — pass that data_source name to "
            "execute_statement. The result also carries statement_best_practices for "
            "each of those data sources: read it before writing a statement, because "
            "each engine has its own language. Use this FIRST to discover what data "
            "exists and where it lives."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural language description of the data you're looking for, "
                        "in the user's own words."
                    ),
                },
                "data_source": {
                    "type": "string",
                    "description": (
                        "Optional: restrict the search to one data source. Leave empty "
                        "to search all of them."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of objects to return (default 8). A question "
                        "spanning two subjects in two data sources needs most of that "
                        "budget, so lower it only for a narrow single-subject lookup."
                    ),
                },
                SUBJECT_ARGUMENT: SUBJECT_PROPERTY,
            },
            "required": ["query"],
        },
    },
    {
        "name": "execute_statement",
        "description": (
            "Execute one read-only statement (a SQL SELECT, for a database) against "
            "one of your connected data sources and return results as JSON. Name it "
            "in the 'data_source' argument — use the name search_schema returned "
            "alongside the definition. The session is read-only: the data source "
            "refuses anything that would change data or objects. Write the statement "
            "in that source's language, following the statement_best_practices that "
            "search_schema returned for it (for example, Oracle has no LIMIT). To "
            "combine data across two data sources, query each one separately and "
            "join the results yourself. A call returns at most max_rows rows (1000 "
            "unless you ask for more, up to the data source's row cap, which "
            "search_schema states). When 'truncated' is true the result is only "
            "part of the answer: call again with the same data_source and statement "
            "and start_row set to the 'next_start_row' it returned, and order by a "
            "unique key so pages neither overlap nor skip rows. Prefer aggregating in "
            "the statement to paging through many rows."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "data_source": {
                    "type": "string",
                    "description": (
                        "Which of your connected data sources to run against, as named "
                        "in the search_schema results."
                    ),
                },
                "statement": {
                    "type": "string",
                    "description": (
                        "The statement to execute, in the language of the data source "
                        "named above."
                    ),
                },
                "start_row": {
                    "type": "integer",
                    "description": (
                        "Rows of the result to skip, for the next page: the "
                        "'next_start_row' of the previous call. 0 or omitted for the first page."
                    ),
                },
                "max_rows": {
                    "type": "integer",
                    "description": (
                        "Rows to return in this call. Omitted means 1000, or the "
                        "data source's row cap if that is lower; at most the row cap."
                    ),
                },
                SUBJECT_ARGUMENT: SUBJECT_PROPERTY,
            },
            "required": ["data_source", "statement"],
        },
    },
]

# web_search, served by its own function (web_search_handler.py).
WEB_TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web for current information that is not in your data sources. "
            "Returns a grounded answer plus the source URLs it cites. Always show the "
            "sources to the user alongside anything you take from the answer. Phrase "
            "the query as a full question."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Maximum number of source URLs to return (default 5, max 10).",
                },
                SUBJECT_ARGUMENT: SUBJECT_PROPERTY,
            },
            "required": ["query"],
        },
    },
]

TOOLS = DATA_SOURCE_TOOLS + WEB_TOOLS
