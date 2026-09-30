"""The model layer.

Everything that talks to a model goes through `client.LLMClient`, so that the
three things Arch §10 requires can be enforced in one place rather than at
every call site: the cap is checked BEFORE the call, a `ModelRun` records what
was sent, and a `CostEvent` records what it cost.
"""
