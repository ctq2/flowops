"""FlowOps — SLA 工单运营平台。

Layering (dependencies point inwards, never outwards)::

    adapters/http  ->  application/services  ->  domain
    adapters/persistence -> application/ports <- domain

``domain`` imports nothing from the rest of the application.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
