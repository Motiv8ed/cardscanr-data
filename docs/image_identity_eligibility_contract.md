# Image identity eligibility contract

Catalogue-record identity, physical-variant identity, and image identity are independent claims. An exact catalogue record requires a stable provider card ID plus language, region where relevant, provider/canonical set identity, printed and normalized collector number, a consistent native name, and no contradictory evidence. A name alone never establishes identity.

Physical state records whether edition, finish, stamp, deck/promo source, regulation mark, parallel foil, or another visible physical difference is resolved. `shared_front_variant_unresolved` is valid when finish remains unknown but the provider exposes one standard catalogue front and no known visible conflict exists.

An image is identity-safe when the catalogue record is exact; language, region, set, collector number, and provider-card mapping agree; the image belongs to that provider record; and there is no known visible variant conflict. Exact finish is not required for catalogue/search representation when finishes share the same front.

Visible stamps, edition marks, different numbering or set symbols, regulation marks, promo/deck branding, combined artworks, and unclear image provenance block assignment. Artwork similarity is corroborating evidence only. Permission, credentials, reachability, and identity safety are separate gates; `imageSafe` never means permission to mirror.
