# Cancelamento e reprocessamento

Entrega OCR-AM-10, LPM `qwslcnBIlzvnmeDgeTjy`. Contrato atual: HTTP 1.10.0
(73 operações) e MCP 0.8.0 (58 ferramentas: 37 leituras, 21 escritas). As 20
ferramentas desta entrega (12 leituras, oito mutações) são o subconjunto OCR
legado. As 12 operações documentais estão em [metadados documentais](document-metadata-api.md);
veja o [contrato HTTP](http-contract.md) e [API/MCP](api-mcp.md) mais recentes.

| HTTP | MCP | Escopo |
|---|---|---|
| POST /api/v1/jobs/{id}/cancel | ocr_cancel_job | jobs:cancel |
| POST /api/v1/jobs/cancel-batch | ocr_cancel_jobs | jobs:cancel |
| POST /api/v1/jobs/{id}/retry | ocr_retry_job | jobs:submit |

## Cancelamento

Cancelar é idempotente sem chave: repetir não cria trabalho novo. A resposta
200 informa `job_id`, `status`, `outcome` e `poll_after_seconds=1`.
Cancelar uma requisição HTTP/MCP ou fechar o cliente não cancela um job.

| Estado no ponto de decisão | Estado devolvido | outcome |
|---|---|---|
| queued | canceled | canceled |
| processing | cancel_requested | requested |
| cancel_requested | cancel_requested | requested |
| canceled | canceled | canceled |
| completed ou failed | preservado | already_terminal |

O claim do worker, cancelamento e publicação do resultado usam a mesma trava
e persistência SQLite. O primeiro a efetivar a transição vence: um job cancelado
não pode ser iniciado; se o pedido vencer a conclusão, o worker não pode publicar
completed. `cancel_requested` ainda ocupa a quota de jobs pendentes, não é terminal
e não libera retry nem artefatos. Consulte o job até o desfecho.

O lote de cancelamento exige `{"job_ids":["..."]}`, com 1–25 IDs distintos.
Cada item informa seu desfecho. IDs alheios e inexistentes têm o mesmo
`outcome=not_found`, sem estado; no endpoint individual ambos retornam 404.
O lote tem efeitos por item, não rollback de todos os cancelamentos. Não há
seleção global implícita. A limpeza de pendentes legada agora compartilha o
compare-and-set e não drena/reinsere a fila em disputa com o worker.

## Interrupção de execução

Há checkpoints entre etapas e antes da publicação, além de polling de 100 ms
durante subprocessos. OCRmyPDF e sondagens realizadas dentro do job usam o
runner controlado. EasyOCR/Poppler executam em subprocesso separado, incluindo
rasterização, carga do modelo e inferência; o processo servidor não carrega o
Reader para executar jobs. O modelo local é recarregado a cada fallback,
trocando reutilização em memória por isolamento.

Em Windows, o runner usa árvore de processos (`taskkill /PID ... /T /F`),
janela oculta e prazo de 10 s para o comando, seguido de espera de até 5 s.
Em POSIX, cria sessão própria, envia SIGTERM ao grupo, espera 200 ms e envia
SIGKILL. Esses prazos se referem ao encerramento do subprocesso. Operações
Python em andamento (por exemplo leitura/construção de PDF) são cooperativas:
o cancelamento permanece solicitado até o próximo checkpoint; não há promessa
de latência máxima para todo o job.

O worker só marca canceled após retornar das etapas e da terminação controlada.
Falha ao confirmar o encerramento mantém cancel_requested com
`process_termination_failed`; exige intervenção operacional.
Após morte abrupta do servidor, cancel_requested é preservado com
`cancellation_recovery_required`, nunca reenfileirado nem declarado encerrado
sem prova. O operador precisa verificar/encerrar processos remanescentes no
host; esta entrega não oferece reconciliação automática desse caso nem promete
limpar descendentes após kill do processo servidor. Um novo upload pode iniciar
trabalho independente depois dessa verificação. Processos queued/processing
sem pedido de cancelamento conservam a recuperação legada.

Fontes de implementação: [subprocess do Python](https://docs.python.org/3/library/subprocess.html)
e [taskkill da Microsoft](https://learn.microsoft.com/pt-br/windows-server/administration/windows-commands/taskkill).

## Reprocessamento

Envie `{}` para herdar as opções solicitadas do original ou
`{"options":{"processing_mode":"extract_text"}}` para substituí-las.
Objetos options são completos com os defaults normais; não são um patch parcial.
Jobs legados sem opções persistidas exigem opções explícitas (`retry_options_required`).

HTTP exige `Idempotency-Key`; MCP recebe `idempotency_key`.
A chave segue 8–128 caracteres ASCII de letras, números, ponto, sublinhado,
dois-pontos e hífen. Identidade + operação + chave são únicas no banco.
Original ou opções diferentes com a mesma chave retornam 409.
Omissão e fornecimento explícito de opções são pedidos distintos.

Só originais completed/failed/canceled podem ser reprocessados.
Entrada original ausente retorna 410 `input_expired`: faça novo upload.
Retenção vencida/exclusão registrada retorna 410 `artifacts_expired` ou
`artifacts_deleted`, mesmo se o purge físico ainda aguarda uma leitura ativa.
A admissão revalida capacidades e quotas atuais e copia a entrada para diretório
novo. Grava job derivado com `parent_job_id` e recibo na mesma transação.
O original e seus resultados não mudam; o novo job também aparece no histórico.
A associação do lote original não se transfere ao derivado.

202 devolve job_id, parent_job_id, status queued e poll_after_seconds.
Replay devolve o recibo inicial, mesmo após conclusão ou expiração da entrada
original; consulte o job para estado atual. Falha no despacho mantém o derivado
persistido e retorna queue_unavailable: repita a chave/payload.
Chaves e vínculo persistem sem TTL; veja a [política de retenção](retention.md).

## Evidência

Testes exercitam 40 corridas claim/cancel e 40 corridas conclusão/cancel,
interrupção entre etapas, término real de processo pai/filho por cancelamento
e timeout, fallback isolado com dependências sintéticas, preservação do original,
16 retries simultâneos, rollback, quotas/escopos e reabertura do banco.
Clientes oficiais MCP HTTP e stdio percorrem cancelamento ativo → retry →
resultado, nos modos auto/legacy, com comparação do recibo HTTP.

Validação local Windows com PDFs digitais e processos/dependências sintéticos.
Não comprova OCR de imagem, PDF/A, modelos reais EasyOCR, Linux/container,
desempenho no NAS ou operação em produção. O painel dedicado ao novo estado
será tratado na atividade de UI.
