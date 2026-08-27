"""ows.gen — LLM-driven embodied scene & task synthesis with programmatic quality gates.

Pipeline: LLM scenario → LLM tasks (+goal/walkthrough) → JSON/schema gates →
compile gate (walkthrough replay) → dedup → staging. Never writes to data/.
"""
