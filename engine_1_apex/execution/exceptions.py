"""Execution layer exceptions for IP4 Apex."""

from __future__ import annotations


class ExecutionHaltedException(Exception):
    """Raised when GLOBAL_EXECUTION_HALT is active."""


class GasSpikeVetoException(Exception):
    """Raised when estimated gas cost exceeds the alpha profit veto fraction."""


class NonceResyncRequired(Exception):
    """Raised when nonce state must be resynced from chain before continuing."""
