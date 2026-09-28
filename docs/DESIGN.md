# SentinelAI — Design decisions

The reasoning behind each choice (CLAUDE.md §12). Filled in during Phase 8.

1. Statistics over trained ML
2. Median/MAD over mean/std
3. Baseline freeze during incidents
4. Hysteresis
5. Minimum volume and absolute floor
6. Patterns and dynamic tokens
7. No seasonality
8. Two-tier AI (statistics decide when; the LLM only explains)
9. Redaction before storage, LLM and AWS
10. Isolation Forest as a second opinion only
