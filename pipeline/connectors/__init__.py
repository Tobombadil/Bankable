"""Sprint 1 connector framework: registry, base contract, polite HTTP, DQ gates, runner."""

from pipeline.connectors.base import Connector, GateViolation, RawSnapshot
from pipeline.connectors.registry import Registry, SourceEntry
from pipeline.connectors.runner import RunResult, run

__all__ = ["Connector", "GateViolation", "RawSnapshot", "Registry", "RunResult", "SourceEntry", "run"]
