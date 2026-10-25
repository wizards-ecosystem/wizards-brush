"""Model and adapter resources: identity, installation and safe local intake.

* ``store``     — persistence of `resource` and `resourceinstall` rows.
* ``catalogue`` — registering configured models and the LoRA library, verifying
  content in bounded background work, and resolving an id to a managed file.
* ``intake``    — resumable upload receipts and the import pipeline: refuse
  unsafe formats, validate structure with bounded reads, deduplicate by
  content, promote atomically into a managed directory.

Identity is the provider identity plus an exact version, or the content hash
of an imported file (studio.contracts.resource_id). A storage location is an
install of a resource and is never shown to clients as a filesystem path.
"""
