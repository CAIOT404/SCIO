<!-- The brief for a W-09 research agent: a change that depends on how a provider, the `claude` CLI or GitHub behaves. Fill every {{slot}}; keep the questions in this order. -->
# Research: {{topic}}

{{the change this research decides, and the issue}}

Documentation and read-only probes only. Run nothing that changes a repository, a setting or
an account. Quote the exact sentence and give the URL for every claim. Where the docs say
nothing, write "not documented"; never guess.

## 1. Can we have it at all? (I-227)
Before any question about how it works: is the feature available to **this** repository and
account? Quote the docs' availability line, and check it against our facts:

- owner type: {{user or organization}}
- plan: {{Free, Pro, Team, Enterprise; or the provider's tier}}
- visibility: {{public or private}}
- surface: {{REST, GraphQL, the CLI, the web UI; or the provider's API or CLI version}}

If it is not available, say so first and stop: the rest of the brief is moot.

## 2. How it behaves
{{numbered questions, one behaviour each}}

## Report
A numbered answer per question, each with its quote and URL, under 400 words, and a last line
listing what the docs did not settle. The orchestrator saves it to `.scio/agents/<issue>.md`
(W-34).
