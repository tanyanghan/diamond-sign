"""Diamond Sign application core.

The runtime pieces the entry point (``bot.py``) wires together: per-server state
storage, log parsing + watching, presence reconciliation, notifications, the
``Server`` runtime object, the chat command layer, and the authorization system.

Like ``utils/``, this package does not re-export from its submodules — import the
specific module you need (``from core.state import ...``). The dependency
direction is strictly layered and acyclic: ``logutil``/``state``/``auth`` are
leaves; ``logparse`` -> ``presence``/``notifications``/``logwatch`` ->
``server`` -> ``commands``; ``bot.py`` is the only sink that imports all of them.
"""
