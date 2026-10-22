# Matriz de clientes e interoperabilidade (OCR-AM-16)

Execução real em 2026-10-22 contra a imagem de produção `sha-957d1ac` (HTTP 1.12.0, MCP 0.10.0), só com PDFs sintéticos. Cada cliente usa uma identidade de serviço própria, então a posse dos trabalhos e o isolamento são reais.

As ferramentas ficam em `scripts/interop/`:
- `matrix.py`: clientes de protocolo;
- `hosts.py`: sessões de hosts MCP reais.

Os resultados, sem segredos, estão em `planejamento/am16-interop-2026-10-22/evidence/`.

## Resultado por cliente

| Cliente | Transporte | Ambiente | Resultado |
| --- | --- | --- | --- |
| HTTP v1 (httpx2) | HTTP | local, loopback | 24/24 |
| HTTP v1 (httpx2) | HTTPS via Cloudflare e proxy | produção | 22/22 |
| SDK MCP, revisão 2026-07-28 | Streamable HTTP | local, loopback | 22/22 |
| SDK MCP, revisão 2025-11-25 (`mode="legacy"`) | Streamable HTTP | local, loopback | 21/21 |
| SDK MCP | stdio, pelo adaptador `scripts/ocr_mcp.py` | local | 22/22 |
| SDK MCP | stdio, pelo adaptador, sobre a API por HTTPS | produção | 20/20 |
| MCP Streamable HTTP pelo hostname público | HTTPS | produção | recusado com `421` nas duas revisões, **por desenho** (ver abaixo) |
| Claude Code 2.1.308, modelo `haiku` | stdio, configuração gerada | local | 12/12, duas sessões |
| agy CLI 1.2.5 → 1.4.5 | stdio, `.agents/mcp_config.json` do workspace | local | 12/12, duas sessões |
| Codex CLI 0.163.0 | stdio, override `-c` | local | **não executado**: a conta atingiu o limite de uso até 27/10/2026 06:23 e o modelo não rodou |
| Claude Desktop, ChatGPT Desktop, Antigravity | stdio | — | **não executado**: só têm interface gráfica, sem modo automatizável; exigem execução assistida pelo owner |
| Hosts remotos com OAuth | `app.remote` | — | **não executado**: depende da AM-13 (decisão do owner sobre o provedor) |

Um host ausente ou não executado **não** conta como aprovado. O agy se atualizou sozinho de 1.2.5 para 1.4.5 entre as duas sessões, e as duas passaram. O problema de descoberta do MCP do agy registrado em 16/10 não se repetiu.

## O que cada cliente de protocolo verifica

Cada cliente de protocolo, local e remoto, executa:

- **Catálogo e identidade:** `tools/list` com 62 ferramentas e a revisão negociada; `whoami` com identidade de serviço; capacidades com só os presets homologados.
- **Fluxo principal:**
  - envio do PDF: a reserva é feita pelo cliente, os bytes vão por HTTP com a mesma identidade, o MCP não carrega bytes;
  - submissão e replay idempotente;
  - acompanhamento até `completed`, com texto, relatório (PDF/A `passed`) e download com o hash validado.
- **Isolamento e permissões:**
  - outro dono não consegue baixar (404), consultar nem cancelar;
  - a listagem nunca mostra o trabalho de outro dono;
  - uma credencial `jobs:read` não consegue reservar upload.
- **Recusas:** preset retirado (`compression_profile_unavailable`) e arquivo que não é PDF (`422`).
- **Controle:** cancelamento de trabalho na fila, retry derivado concluído e exclusão de artefatos (`410` depois).
- **Reinício do servidor** (só local): o trabalho admitido antes termina, e o replay devolve o mesmo trabalho numa sessão nova.
- **Genéricos:**
  - `401` sem token e com token inválido, nos dois transportes, com `X-Request-ID`;
  - `429` com `Retry-After` por principal, sem afetar outro principal;
  - o log do adaptador stdio não contém tokens.

## O que cada host verifica

Cada host roda duas sessões não interativas com a configuração gerada por `app.mcp_client_config`, sem editar a configuração global.

**Sessão 1**, seguindo um roteiro fixo:
- `whoami` e capacidades;
- submissão de um upload preparado;
- acompanhamento até o fim, texto e relatório;
- consulta a um trabalho de outro dono, que deve dar erro;
- submissão seguida de cancelamento e retry;
- exclusão dos artefatos do trabalho cancelado.

O servidor confere de forma independente:
- que o trabalho pertence ao principal do host;
- que o marcador do texto e o hash do relatório informados pelo host batem;
- os estados `canceled`, `completed` e `deleted`;
- que o trabalho do outro dono continua intacto.

**Sessão 2**, num processo novo depois de reiniciar o servidor: o host relê o trabalho e a listagem.

## Achados

- **MCP pelo hostname público:** o `/mcp` do app principal aceita só host de loopback, por proteção contra DNS rebinding. Pelo hostname público ele responde `421 Invalid Host header`. É o comportamento esperado: o MCP remoto é o entrypoint OAuth `app.remote`, que fixa o próprio host (AM-13). Até lá, o caminho remoto suportado é o adaptador stdio na máquina do usuário, consumindo a API por HTTPS. Esse caminho passou em produção.
- **Teste de 429 remoto:** o limite de 600 por minuto se recompõe a 10 por segundo. Requisições sequenciais através da Cloudflare (cerca de 6,6 por segundo) nunca o esgotam. O teste dispara em paralelo, e o 429 aparece como esperado.

## Como reproduzir

Servidor local com a imagem de produção e dados vazios. As credenciais de administrador ficam num env-file privado, nunca em argv:

```sh
docker run -d --name ocr-am16 --env-file <server.env> -p 127.0.0.1:18080:8000 \
  -v <data>:/tmp/ocr-recognizer localhost/ocr-recognizer:sha-<tag>
python scripts/interop/matrix.py --api http://127.0.0.1:18080 --admin-env <server.env> --out <dir> \
  --restart "docker restart ocr-am16" --rate-limit 120
python scripts/interop/hosts.py --api http://127.0.0.1:18080 --admin-env <server.env> --out <dir> \
  --hosts claude-code,codex,agy --restart "docker restart ocr-am16"
```

O `server.env` define `JWT_SECRET`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` e `PRINCIPAL_REQUESTS_PER_MINUTE=120`.

Em produção, as identidades são temporárias:
- são emitidas dentro do pod pelo contrato HTTP, com credenciais de 1 h;
- são usadas com `--tokens-file ... --public-mcp-refused --rate-limit 600`;
- são desativadas logo depois;
- os scripts estão em `planejamento/am16-interop-2026-10-22/scripts/`.

## Revisão independente

Revisão pelo Nemotron (OmniRoute). Cinco reforços contra falsos positivos foram aplicados e tudo foi executado de novo localmente (ver `review-disposition.md` nas evidências):
- código aleatório impresso só no PDF, que o host precisa devolver;
- sessão 2 conferida com valores que só o servidor conhece;
- `job_id` do host conferido;
- varredura de vazamento em todos os arquivos, incluindo o token de administrador;
- código exato da recusa do preset.

A rodada remota é anterior ao último reforço. Nela, a recusa do preset pelo stdio foi registrada como erro, mas sem o código. O código `compression_profile_unavailable` foi confirmado pelo HTTP remoto e pelo stdio local, com a mesma imagem.

## Limites

- **Codex:** pendente até a cota da conta voltar (27/10).
- **Hosts gráficos e OAuth:** os três hosts gráficos e o OAuth remoto (AM-13) ficam fora desta execução.
- **Modelos e prompts:** as sessões de host usam modelos dos próprios provedores, com conteúdo só sintético. O roteiro é fixo, e o servidor confere o resultado: não se confia na resposta do modelo.
- **Ambiente:** o servidor local roda a mesma imagem de produção, mas não o mesmo hardware. Concorrência e carga ficam fora do escopo.
