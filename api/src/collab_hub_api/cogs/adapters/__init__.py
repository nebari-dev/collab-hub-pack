"""Registry adapters: the only place vendor-specific code may live.

Each module implements ``cogs.registry.RegistrySource`` for one registry
product (or, for ``static``, for none). Everything an adapter does beyond
enumeration, credential shape, and webhook translation belongs in the generic
OCI client instead. Nothing outside this package may import from a vendor
adapter except the dispatch in ``cogs.registry.build_registry_sources``.
"""
