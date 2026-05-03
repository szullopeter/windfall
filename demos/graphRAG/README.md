# GraphRAG — AI Copyright & Governance Knowledge Graph

An end-to-end GraphRAG pipeline that scrapes web content about AI intellectual property and copyright, extracts entities and relationships using LLMs, builds a queryable knowledge graph, and renders it as an interactive visualization.

## Overview

This project combines web scraping, LLM-powered entity extraction, graph analysis, and interactive visualization to build a domain-specific Retrieval-Augmented Generation (RAG) system focused on AI copyright and governance topics.

## Tech Stack

| Layer | Tools |
|-------|-------|
| LLM / RAG | LlamaIndex, OpenAI (gpt-4o-mini, gpt-4o) |
| Web scraping | SerpAPI, Trafilatura, YouTube Transcript API |
| Graph analysis | NetworkX, Graspologic (Louvain community detection) |
| Visualization | D3.js v7, vis-network 9.1.2 |
| Data | Pandas, Pydantic |


## Project Structure

```
graphrag/
├── scrape_info.ipynb              # Web scraping & text enrichment
├── graphrag_ai_copyright.ipynb    # GraphRAG pipeline, queries & visualization
├── ai_copyright_dataset.csv       # Scraped articles/videos (810 rows)
├── graph_data.json                # Extracted knowledge graph (nodes + edges)
├── ai_copyright_graph.html        # Interactive visualization (generated output)
├── graph_template.html            # HTML/D3.js template for visualization
├── .env                           # API keys (not committed)
└── lib/
    ├── bindings/utils.js          # Graph interaction utilities
    ├── vis-9.1.2/                 # vis-network library
    └── tom-select/                # Dropdown UI component
```

## Prerequisites

- Python 3.10+
- A [SerpAPI](https://serpapi.com/) key (Google Search)
- An [OpenAI](https://platform.openai.com/) API key
- API key variables added in the `.env` file

## Setup

1. **Clone the repo and create a virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install llama-index llama-index-llms-openai graspologic \
               pandas nest-asyncio python-dotenv \
               google-search-results trafilatura youtube_transcript_api requests
   ```

3. **Configure API keys** — create a `.env` file in the project root:
   ```
   SERPAPI_KEY=your_serpapi_key_here
   OPENAI_API_KEY=your_openai_api_key_here
   ```

## Usage

### Step 1 — Scrape content (`scrape_info.ipynb`)

Searches Google for AI copyright/IP articles and YouTube videos, then enriches them with full article text (via Trafilatura) and video transcripts (via YouTube Transcript API).
You can swap out the search queries with any other search terms relevant to your project.

Output: `ai_copyright_dataset.csv`

### Step 2 — Build the knowledge graph (`graphrag_ai_copyright.ipynb`)

Loads the scraped dataset and runs the full GraphRAG pipeline:

- **Entity extraction** — uses GPT4o to extract entities and relationships (under the types defined in ontology).
- **Relationship extraction** — maps typed relationships between entities using Pydantic-validated schemas
- **Community detection** — runs Louvain clustering to group related entities into thematic communities
- **Community summarization** — generates LLM summaries for each cluster
- **Query engine** — answers complex questions by synthesizing context across communities using `gpt-4o`
- **Export** — writes `graph_data.json` and renders `ai_copyright_graph.html`

### Step 3 — Explore the visualization

Open `ai_copyright_graph.html` in a browser. Features:

- Force-directed graph layout (D3.js)
- Filter nodes by entity type via the sidebar legend
- Search nodes by name
- Click a node to highlight its direct connections
- Hover for entity details in a tooltip
- Adjust link distance with the slider
