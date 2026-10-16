# Clientes MCP — contrato local e homologação

Alvos confirmados pelo usuário: Antigravity, Claude Desktop, Claude Code,
ChatGPT Desktop, Codex CLI e agy CLI. O caminho local comum é **stdio**, com o
adaptador autenticado consumindo a API HTTP. Não exige que cada host interprete
os campos de configuração HTTP da mesma maneira. O servidor também fornece
Streamable HTTP em `/mcp`; OAuth remoto e publicação HTTPS pertencem à AM-13.

## Gerar configuração revisável

No checkout com as dependências instaladas:

```powershell
python -m app.mcp_client_config --client claude-code `
  --python C:\OCR\.venv\Scripts\python.exe `
  --api-url http://127.0.0.1:8000 `
  --token-file C:\OCR\private\service-token.txt `
  --output C:\OCR\generated\claude-code
```

Valores de `--client`: `claude-desktop`, `claude-code`, `antigravity`, `agy`,
`codex`, `chatgpt-desktop`. O diretório de saída deve ser novo. O comando não lê
o segredo nem edita configurações de clientes. O arquivo de credencial deve
conter somente um Bearer válido; proteja-o com permissões do usuário do processo.
O adaptador aceita `OCR_API_TOKEN` **ou** `OCR_API_TOKEN_FILE`, nunca ambos com
valor. As configurações geradas limpam o primeiro e apontam para o segundo.
Use uma credencial de serviço provisionada fora da conversa e com escopos mínimos.

O launcher `scripts/ocr_mcp.py` é absoluto e funciona com cwd diferente do projeto.
Ele precisa deste checkout e do Python/dependências informados. URLs de API só
podem ser origens HTTPS ou HTTP loopback, sem credenciais, caminho ou query.
O processo stdio não abre banco nem worker; stdout pertence exclusivamente ao MCP.

| Cliente | Artefato gerado / instalação a revisar |
|---|---|
| Claude Desktop | `claude-desktop.json`: mesclar somente a entrada em `mcpServers` na configuração do aplicativo |
| Claude Code | `claude-code.json`: configuração MCP no escopo desejado; teste local usa `CLAUDE_CONFIG_DIR` isolado |
| Antigravity | `antigravity.json`: entrada `mcpServers` em `.agents/mcp_config.json` do workspace ou configuração global suportada |
| agy CLI | `agy.json`: mesmo formato; alternativa oficial `agy mcp add --env ... nome comando args` escreve configuração e deve ser executada conscientemente |
| Codex CLI | `ocr-config.toml`: seção `mcp_servers.ocr-recognizer`, sem substituir as outras seções do usuário |
| ChatGPT Desktop | Pasta `ocr-recognizer` com `plugin.json` e `mcp.json`, stdio e `requires_local_executor`; instalar pelo fluxo local suportado pelo host |

O pacote do ChatGPT é uma configuração **para a máquina que o gerou**, com
referências absolutas ao runtime e ao checkout. Não é um ZIP público portátil,
não empacota Python e não instala automaticamente em uma conta. Credenciais e
arquivos do processo não fazem parte do pacote. Web/mobile não executam esse
processo local. O gerador também não altera configurações globais dos outros hosts.

## Evidência local e limites (16/10/2026 UTC)

| Cliente/camada | Resultado observado | Ainda necessário para homologação completa |
|---|---|---|
| SDK MCP 2.3.0 | Inicialização, 20 tools, whoami e capabilities pelo launcher/arquivo de token em modos auto e legacy; suíte vertical também cobre HTTP/stdio, upload e resultados | Não equivale ao comportamento de seis aplicativos |
| Claude Code 2.1.300 | `mcp get ocr-recognizer` retornou Connected com API sintética e configuração isolada | Invocação de tools em sessão do aplicativo e políticas de aprovação |
| Codex CLI 0.162.1 | Parser real `mcp get --json` aceitou configuração por override apenas naquela chamada | Descoberta e invocação de tools em sessão |
| agy CLI 1.2.5 | Gerador/JSON válidos, mas `mcp list` não exibiu a entrada de workspace no experimento isolado; teste marcado xfail | Resolver descoberta no contexto de projeto/sessão e executar tools |
| Antigravity / Claude Desktop | Configuração gerada para stdio; transporte comum testado pelo SDK | Importar no aplicativo, listar catálogo e executar tools |
| ChatGPT Desktop | Manifestos de plugin local validados contra os schemas oficiais; transporte comum testado pelo SDK | Validar instalação, catálogo, chamadas e aprovações na versão instalada |

AM-16 deve registrar, **para cada host**, versão, transporte, initialize,
tools/list, whoami, capabilities, PDF sintético, consulta de texto/relatório,
download, negativa por escopo/dono, cancel/retry/delete e reinício. Nunca fechar
essa matriz com apenas validação de JSON, parser ou handshake. A ausência de um
host não é um teste aprovado. Não houve chamada paga de modelo nesta validação.

## Documentação oficial consultada

- [Claude Code MCP](https://code.claude.com/docs/en/mcp): stdio, command/args/env e escopos de configuração.
- [Antigravity MCP](https://antigravity.google/docs/mcp) e [CLI MCP](https://antigravity.google/docs/cli/mcp): configurações de projeto e globais; confirmar comportamento da versão instalada.
- [Codex CLI MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli): configuração TOML e timeouts.
- [ChatGPT Desktop plugins locais](https://developers.openai.com/plugins/deploy/desktop-only): execução local via stdio.
- [Schema plugin](https://agent-plugins.org/schemas/1.0.0/plugin.schema.json) e [schema MCP](https://agent-plugins.org/schemas/1.0.0/mcp.schema.json): formato dos manifestos portáveis.
