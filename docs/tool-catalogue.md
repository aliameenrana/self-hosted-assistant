# Tool Catalogue

Ordered by how commonly they appear in general-purpose agents. Mark the ones
you want. Nothing here is built yet except the five marked SHIPPED.

Retrieval means we can hold a large catalogue and still show the model only
the two or three tools relevant to the message, which keeps selection accuracy
high. See "Dynamic selection" at the end.

Risk column assumes a public demo where a successful injection controls every
argument.

## Tier 1: near universal

| # | Tool | What it does | Risk | Status |
|---|---|---|---|---|
| 1 | `search_web` | Query the web, return titles, URLs, summary | low | SHIPPED |
| 2 | `read_url` | Fetch and extract text from a page | medium, SSRF | SHIPPED |
| 3 | `get_datetime` | Current date and time | none | SHIPPED |
| 4 | `calculate` | Exact arithmetic | none | SHIPPED |
| 5 | `create_webpage` | Publish an HTML artifact, return a link | low, sanitised | SHIPPED |
| 6 | `read_document` | Extract text from an uploaded PDF, DOCX, TXT | medium, parser CVEs | SHIPPED |
| 7 | `run_python` | Execute code in a sandbox | **HIGH** | not built |
| 8 | `search_memory` | Full text search over this user's past chats | low | backend exists, not a tool |
| 9 | `remember_fact` | Explicitly store something durable | low | backend exists, not a tool |
| 10 | `read_file` / `list_files` | Workspace file access | **HIGH** on a server | not built |

## Tier 2: common

| # | Tool | What it does | Risk | Notes |
|---|---|---|---|---|
| 11 | `create_chart` | Render a chart to an image or artifact | low | pairs well with 5 |
| 12 | `analyse_data` | Load a CSV, compute stats, return a summary | medium | needs 7 or a narrow DSL |
| 13 | `generate_image` | Text to image | low, needs a second model | ~4GB VRAM you do not have spare |
| 14 | `summarise_url` | Fetch and condense in one call | low | consolidation of 1 and 2 |
| 15 | `extract_structured` | Pull JSON matching a schema from text | none | pure prompting, cheap and useful |
| 16 | `translate` | Between languages | none | the model already can, tool adds little |
| 17 | `send_email` | Send a message | **HIGH**, outbound spam | never on a public demo |
| 18 | `create_reminder` | Schedule a future notification | medium | needs a scheduler |
| 19 | `browse_page` | Headless browser, click and read JS pages | **HIGH** | full browser is a big surface |
| 20 | `query_database` | Read-only SQL against a known schema | medium | fine if parameterised and read-only |

## Tier 3: specialised

| # | Tool | What it does | Risk | Notes |
|---|---|---|---|---|
| 21 | `read_repo` | Browse a public git repo | low | GitHub API, read only |
| 22 | `run_tests` | Execute a test suite | **HIGH** | needs 7 |
| 23 | `diff_text` | Structured diff of two blocks | none | trivial, useful for CV and doc review |
| 24 | `ocr_image` | Text out of an image or scanned PDF | medium | fixes "scanned CV" gap |
| 25 | `transcribe_audio` | Speech to text | low, needs whisper | another model in VRAM |
| 26 | `convert_document` | Between formats, md to pdf, docx to md | medium | pandoc is a large surface |
| 27 | `fetch_rss` | Read a feed | low | narrow, safe version of 2 |
| 28 | `geocode` / `weather` | Location and forecast | low | needs an API key |
| 29 | `unit_convert` | Physical units, currency | low | currency needs a live rate |
| 30 | `spell_grammar` | Proofread a passage | none | the model already can |

## What I would pick, and why

**Build next, high value and low risk:**
- 8 and 9, memory as tools. The backend exists. Makes recall visible in the
  glass box and lets the model decide when to look something up rather than
  always carrying context.
- 15 `extract_structured`. Pure prompting, no new surface, immediately useful
  for CV and document work.
- 23 `diff_text`. Trivial to build, makes document review much stronger.
- 11 `create_chart`. Reuses the artifact pipeline you already have.

**Deliberately not, on a public demo:**
- 7 `run_python`. The single largest risk in the list. It is the one tool that
  turns a prompt injection from a nuisance into a compromise. If you want it,
  it needs a container with no network, a read-only filesystem, a memory and
  CPU cap, and a hard timeout, and even then it is the weakest point in the
  system. Worth doing later for the VS Code extension where the user is
  running it on their own machine and their own code.
- 17 `send_email`. An injection turns your laptop into a spam relay.
- 19 `browse_page`. A headless browser reintroduces every SSRF and local
  network risk that `read_url`'s allowlist currently blocks.
- 13, 25. Each needs several GB of VRAM you do not have spare.

## Dynamic selection

With more than roughly eight tools, a small model's router accuracy drops
noticeably. The fix is retrieval: embed every tool description once, embed the
incoming message, and pass only the top k tools to the model. Reported
Recall@5 in production deployments is 0.83 to 0.92.

Design for this project:

1. Embed all tool descriptions at startup with a small sentence embedding
   model, roughly 90MB, or with cheap TF-IDF to start.
2. On each message, embed the text and take the top 3 by cosine similarity.
3. Always include `search_web` and `get_datetime` regardless, they are cheap
   and frequently needed.
4. Log which tools were offered and which was chosen, so the retrieval itself
   can be measured.

The known failure mode is vocabulary mismatch: a user says "how much is" and
the tool says "arithmetic". Mitigate by writing descriptions in user language
and adding example phrasings to each tool's embedded text.

**Built, and measured.** At eleven tools the lexical hybrid wins outright:

| | Top-1 | recall@k | per query | cost |
|---|---|---|---|---|
| lexical BM25 + TF-IDF | 88% | 100% | 0.03ms | none |
| MiniLM-L6-v2 embeddings | 83% | 88% | 5ms | 1GB venv, 11GB cache, 162s load |

The published numbers favour embeddings (38% vs 21% Top-1) but were measured
over 270 to 2,792 tools. At this scale there is nothing to disambiguate, and
writing trigger words in user language moved Top-1 further (62 to 88 percent)
than any change of algorithm.

Revisit if the catalogue passes a few hundred tools.
