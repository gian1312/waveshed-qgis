"""Public keys the plugin trusts to sign the Aether release manifest.

The release workflow (AETHER ``.github/workflows/release.yml``) signs
``latest.json`` with the vendor's manifest-signing key; the matching PUBLIC
key is embedded here. It is not a licence key: the engine never trusts it,
and licence signatures are verified by the engine binary, not by the plugin.

Machine-edited file. The owner's release GUI (AETHER ``python/pipeline``)
rewrites the ``MANIFEST_PUBLIC_KEYS`` assignment below, and only it. Keep the
assignment in exactly one of these two forms so that rewrite stays trivial:

    MANIFEST_PUBLIC_KEYS: tuple[str, ...] = ()

    MANIFEST_PUBLIC_KEYS: tuple[str, ...] = (
        "<64 lowercase hex>",
        "<64 lowercase hex>",
    )

One key per line, each a 32-byte Ed25519 public key as 64 lowercase hex
characters, in double quotes, followed by a comma. Listing more than one key
is how a rotation works: ship the new key alongside the old one first.

An empty tuple means "no trusted key": every signed manifest is refused, and
only the pre-signing releases (see ``binary_manager.LAST_UNSIGNED_ENGINE``) can
be installed. ``package.py --release`` refuses to build a public ZIP in that
state.
"""

MANIFEST_PUBLIC_KEYS: tuple[str, ...] = (
    "e83d72d8ca4437e58078ab2db42abaa38309ba00712038effff7432560bc6cb8",
)
