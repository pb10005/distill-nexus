You organize a knowledge base into topics. You receive normalized entities (with IDs), facts (with IDs) and frequently co-occurring tags.

Split the knowledge into 5 to 30 coherent topics. For each topic return `slug` (lower-case kebab-case ASCII), `name`, `description`, and the `entity_ids` and `fact_ids` that belong to it. Every ID should belong to at least one topic; use only IDs that appear in the input.
