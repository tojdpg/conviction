# MCP / Discord agents

Conviction's native stdio MCP server is auto-injected by Hermes into configured
Discord profile conversations. Its tools deliberately let agents **read
signals** and **post an investment thesis**; they do not trade or mutate
portfolio positions.

When the Conviction backend uses HTTP Basic Auth, configure the MCP process
with `CONVICTION_AUTH_FILE` pointing to a private, mode-restricted `KEY=VALUE`
file containing `CONVICTION_AUTH_USERNAME` and `CONVICTION_AUTH_PASSWORD`.
Never put those credentials in Hermes configuration or Git.
