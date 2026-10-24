"""Studio presets: the versioned creative setups above the job registry.

A preset names a model and adapters by resource identity, composes prompts
from declared fragments, and supplies settings defaults. Running one never
adds a queue or a job kind: the resolver turns it into an immutable execution
plan, and the plan is submitted as an ordinary job of an existing kind.

See docs/studio-contracts.md for the vocabulary and why each name was chosen.
"""
