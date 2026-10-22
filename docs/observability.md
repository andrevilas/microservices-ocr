# Observabilidade, limites e logs sem conteúdo (OCR-AM-15)

## Correlação: um `request_id` do pedido ao job

- **HTTP v1 (`/api/v1/*`).** O servidor gera o ID e o devolve em `X-Request-ID` e no corpo dos erros. IDs enviados pelo cliente são ignorados.
- **MCP (`/mcp`).** Cada requisição HTTP do MCP recebe seu próprio ID, devolvido em `X-Request-ID`, inclusive nas respostas 401 e 429. A ferramenta roda com esse mesmo ID.
- **Jobs.** O job grava o ID do pedido que o admitiu na coluna `jobs.origin_request_id`. Jobs legados e recuperados ficam com `NULL`.
  - O worker de OCR processa e registra logs com `request_id=<origem> job_id=<job>`.
  - A extração assistida herda o ID do pedido que a criou.
  - A linha `Job queued` associa os dois IDs no momento da admissão.

Para investigar, procure o `X-Request-ID` recebido pelo cliente nos logs do pod:

```sh
k3s kubectl -n ix-ocr-recognizer logs deploy/ocr-recognizer-ix-chart | grep 'request_id=<id>'
```

Isso mostra a requisição, o job admitido e o processamento do job.

Formato de cada linha:

```
<data> <nível> <logger> request_id=<id|-> job_id=<id|-> <mensagem>
```

## Limite de requisições por principal

`PRINCIPAL_REQUESTS_PER_MINUTE` define o limite por minuto. O valor padrão `0` desliga o limite.

- O limite usa um balde de fichas por identidade autenticada: `user:<id>` ou a identidade de serviço.
- Vale para HTTP v1, MCP e as rotas legadas autenticadas com Bearer, todos debitando do mesmo balde.
- Acima do limite, a resposta é `429 rate_limited` com `Retry-After` em segundos.
- As cotas que já existiam continuam valendo: jobs pendentes, reservas e armazenamento de upload, exportações, contextos e tipos, e o orçamento diário de tokens da extração. Todas são por dono.

Valor em produção: `600` por minuto. Ele não afeta o uso humano nem o smoke e corta laços de clientes MCP.

## Logs sem conteúdo nem credenciais

`app/observability.py` instala um único handler no logger raiz e protege também os handlers do uvicorn:

- **Exceções.** Os logs de exceção mostram só os tipos e os locais no código (`arquivo:linha função`) da cadeia de causas. As mensagens de exceção, que podem conter texto de documento, nunca são escritas.
- **Segredos.** São mascarados `Bearer`/`Basic`, `ocr_svc_*`, JWTs, `password=`, `token=`, `api_key=`, `client_secret` e endereços de e-mail.
- **Tamanho.** Cada linha é cortada em 2000 caracteres.
- **Log de acesso.** A query string é removida (`?[query-redacted]`), porque filtros como `filename=` são dados do documento.
- **Teste de guarda.** `tests/test_observability.py` falha se uma chamada de log receber como argumento algo chamado `text`, `content`, `filename`, `email`, `token`, `prompt` e afins, ou se usar f-string.

Para ajustar o nível, use `LOG_LEVEL` (padrão `INFO`).

## Vigia do TrueNAS

`deploy/truenas-apps/scripts/ocr-watchdog.py` usa só a biblioteca padrão e roda pelo cron do TrueNAS a cada 15 minutos. Ele lê o estado sem gravar nada e nunca toca em documentos, resultados de extração ou tokens.

| Verificação | Alerta |
| --- | --- |
| Pods `ix-ocr-recognizer` e `document-rag` | `pod_missing`, `pod_not_ready`, `pod_restarted` |
| Último relatório da sincronização do RAG | `sync_failed`, `sync_attention`, `sync_stale` (mais de 26 h), `sync_never_ran` |
| LLM da extração pela tailnet (`/v1/models`) | `llm_unreachable` |
| Extrações nas últimas 24 h | `extraction_failing` (3 ou mais falhas de provedor e pelo menos metade das execuções) |
| Outbox do RAG | `rag_delivery_resolution`, `rag_delivery_stuck` (mais de 1 h) |
| Jobs em processamento | `jobs_stuck` (mais de 30 min sem atualização) |
| Disco do dataset de dados (cota ZFS) e memória do host | `disk_low` (menos de 15% da cota ou de 1 GB livres), `memory_low` (menos de 300 MB) |

O vigia grava dois arquivos em `/mnt/NVME/ocr-apps/monitor/`:

- `status.json`: o estado atual, reescrito a cada execução;
- `alerts.jsonl`: uma linha por mudança de estado (`raised`/`cleared`).

O código de saída é 1 enquanto houver alerta ativo.

**Entrega.** `--notify-command`, ou a variável `OCR_WATCHDOG_NOTIFY`, recebe as transições em JSON pela entrada padrão. O canal de entrega (e-mail com SMTP configurado no TrueNAS, mensagem ou outro) é decisão do owner. Sem canal, os alertas ficam nos arquivos acima.
