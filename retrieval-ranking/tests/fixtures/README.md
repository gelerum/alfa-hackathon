# Synthetic database fixtures

- `schema.sql` is an unchanged copy of the shared database schema.
- `schema-provenance.json` records its repository, commit, and SHA-256.
- `seed.py` creates synthetic companies, immutable ready contexts, and news revisions.
- Vectors are artificial 1024-dimensional basis vectors. They verify SQL selection and provenance; they do not measure retrieval or ranking quality.
- Profiles are inserted as drafts and published after their facets, matching shared-schema triggers.
- Dates, stale versions, duplicates, wrong configurations, and missing embeddings are intentional regression cases.
- Integration tests create and drop only uniquely named `retr_rank_test_*` databases on port 55439.
- The optional `scripts/test_database.py demo` command creates only the marked `retr_rank_demo` database. Its `--reset` option refuses to replace an unmarked database.
