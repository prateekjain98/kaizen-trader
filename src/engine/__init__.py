"""Real-time Claude-powered trading engine.

Architecture:
    DataStreams  →  SignalDetector  →  ClaudeBrain  →  Executor
    (free APIs)     (rule-based)      (LLM, selective) (Binance)

Components:
    data_streams.py  — Real-time data ingestion from all free APIs
    signal_detector.py — Fast rule-based signal generation
    claude_brain.py  — Claude API for analysis and decisions
    executor.py      — Binance order execution
    runner.py        — Main event loop connecting everything
"""
