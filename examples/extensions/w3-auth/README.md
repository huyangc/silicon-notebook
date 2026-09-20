# W3 authentication provider example

[中文](./README_zh.md)

This is an independently packageable deployment extension for Silicon
Notebook's `auth.provider` point. It has no frontend package and exposes no
HTTP route. Core owns the public start/callback endpoints, OAuth state,
browser proof, PKCE values, target local account, identity mapping and site
session. The plugin receives only one authorization code exchange and returns
`ExternalIdentity(provider_namespace, subject, username, display_name)`.

The checked-in TOML is disabled. To evaluate it, install this directory into
the backend interpreter, copy `extensions.example.toml` outside the checkout,
export `W3_LOGIN_ORIGIN`, `W3_CLIENT_ID`, `W3_CLIENT_SECRET`, and optionally
`W3_CA_BUNDLE`, then enable the copied entry and select it through core's auth
policy. Never place the secret or a private endpoint directly in committed
TOML. The login origin must be HTTPS; the optional CA variable names a PEM
bundle trusted by `httpx`. Redirects are disabled and every request is bounded
by both the plugin timeout and the host's remaining deadline.
Token and userinfo response bodies are each read to at most 256 KiB, including
chunked responses without a `Content-Length` header.

The adapter implements the observed W3 flow:

- authorize: `GET /saaslogin1/oauth2/authorize` with `response_type=code`,
  `scope=base.profile`, and `display=page`;
- token: JSON `POST /saaslogin1/oauth2/accesstoken` with the code, client
  credentials (`client_id` / `client_secret`), fixed callback, and optional PKCE
  verifier;
- userinfo: `GET /saaslogin1/oauth2/userinfo`, using the documented query-token
  form by default or a deployment-confirmed Bearer header mode.

`uid` must be a non-empty JSON string and always becomes `username`.
`displayNameCn`, then `displayName`, then `uid` supplies the display name.
The default `subject_field` is also `uid`. **Do not deploy that default until
the provider confirms that `uid` is unique within `provider_namespace`, stable
across rename/client upgrades, and never reassigned.** If W3 supplies a
separate immutable identifier, explicitly configure that top-level field as
`subject_field`; changing it or the namespace requires an identity migration,
not an in-place plugin upgrade.

`pkce_supported=false` records the current unknown rather than guessing. Turn
it on only after the provider and registered callback have been verified.
Likewise, choose `userinfo_auth_mode="bearer"` only after W3 confirms it. The
query mode is confined to the fixed backend HTTPS endpoint; the plugin never
returns that URL, token, provider response, or exception text to core.

The package imports only `app.extension_sdk` plus its own dependencies. Core
never imports this example, so deployments without the package retain the
empty optional provider topology. Its manifest uses the current
`EXTENSION_API_VERSION`; an incompatible host rejects it during discovery.
