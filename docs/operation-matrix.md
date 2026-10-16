# Reconciliação das 29 funcionalidades — OCR-AM-12

Fonte: matriz do plano OCR-AM-01, projeto LPM `POzf83SdXU3HWL9KX9i2`.
Esta tabela usa os nomes efetivamente implementados; nomes propostos diferentes
não são aliases. Catálogo atual: HTTP v1 **1.6.0, 30 operações**; MCP **0.5.0,
20 tools**. Contratos e permissões: [API/MCP](api-mcp.md).

| ID | Funcionalidade | Contrato efetivo / decisão de cobertura |
|---|---|---|
| F01 | Um PDF | upload-sessions + PUT content + POST jobs; ocr_prepare_upload/get_upload/submit_job; binário fora de tools |
| F02 | Lote | POST/GET batches; ocr_create_batch/list_batches/get_batch; admissão atômica com manifesto |
| F03 | Pasta local | Navegador enumera PDFs e envia uploads/lote; caminho relativo só na aba. UI envia até o limite por lote/cota pendente e mantém o restante na lista para o envio seguinte (sem sequenciamento automático). Helper local opcional não implementado; servidor/MCP não recebe raiz de disco |
| F04 | Histórico e filtros | GET jobs / ocr_list_jobs; cursor e filtros status/nome/data/lote/qualidade. UI restaura todas as páginas e filtra localmente por estado, nome, período, qualidade e lote (GET batches) |
| F05 | Job/progresso | GET jobs/{id} / ocr_get_job; estados e progresso do backend |
| F06 | PDF | GET jobs/{id}/artifact e /artifact/content; ocr_get_artifact retorna referência autenticada/checksum; host baixa por HTTP |
| F07 | ZIP | POST exports, GET exports/{id} e /content; ocr_create_export/get_export; assíncrono, manifesto e expiração |
| F08 | Texto | GET jobs/{id}/text / ocr_get_text, página/offset/max_chars |
| F09 | Relatório | GET jobs/{id}/report / ocr_get_report; qualidade heurística, opções e execução |
| F10 | Opções | POST jobs/batches/retry + capabilities / submit_job/create_batch/retry_job/get_capabilities; perfis permitidos |
| F11 | Cancelar pendente | POST jobs/{id}/cancel / ocr_cancel_job |
| F12 | Cancelar selecionados | POST jobs/cancel-batch / ocr_cancel_jobs; até 25 IDs explícitos |
| F13 | Cancelar processamento | Mesmo contrato de F11, estado cancel_requested e cancelamento cooperativo |
| F14 | Reprocessar | POST jobs/{id}/retry / ocr_retry_job; novo ID e parent_job_id |
| F15 | Excluir arquivos | DELETE jobs/{id}/artifacts / ocr_delete_job_artifacts; terminal, tombstone, idempotência |
| F16 | Métricas/fila | GET metrics / ocr_get_metrics; fila consultada por GET jobs?status=queued e ocr_list_jobs(status=queued), sem endpoint/tool redundante queue. UI mostra o resumo de GET metrics e o filtro "Em andamento" |
| F17 | Saúde/capacidades | /health, /readiness; GET capabilities / ocr_get_capabilities distingue disponibilidade OCR. UI mostra motores, motivos, avisos e limites; /health e /readiness permanecem operacionais |
| F18 | Identidade | GET me / ocr_whoami |
| F19 | Perfil | HTTP legado PUT /api/me e tela de conta preservados; perfil v1/tool: AM-14 |
| F20 | Autenticação/senha | HTTP /auth/* e conta existentes; bootstrap browser-session same-origin. Senhas fora de tools. OAuth/revogação remota: AM-13 |
| F21 | Consultar usuários | HTTP legado /api/users; contratos v1/permissões e catálogo administrativo: AM-14 |
| F22 | Editar cadastro | HTTP legado /api/users/{id}; perfil administrativo v1/tool: AM-14 |
| F23 | Criar usuário/senha | HTTP legado /api/users; fluxo administrativo seguro e sem segredos em MCP: AM-14 |
| F24 | Credenciais de integração | API v1 /service-principals e credenciais já implementada em AM-03; tela administrativa /admin/integrations usa as mesmas 6 operações (segredo exibido uma vez). Catálogo MCP de metadados/revogação: AM-14; emissão fora da conversa |
| F25 | Lista/detalhes/seleção | Apresentação local; detalhes consomem v1; seleção alimenta ZIP e cancelamento; nenhuma tool de clique |
| F26 | Reset visual | Apenas aba (menu Visualização); nunca cancela ou exclui. Cancelamento e destruição têm ações separadas e confirmadas |
| F27 | Retenção | Política/deadline no contrato, coletor periódico, leases e 410; sem limpeza genérica de disco via tool |
| F28 | Deploy/backup/shell | Operação de infraestrutura fora da API do produto e do catálogo MCP |
| F29 | Webhooks/URL/tabelas | Extensões futuras do plano, sem promessa de disponibilidade |

AM-12 fecha a paridade operacional do fluxo OCR e a migração do painel. Não
antecipa AM-13/14/16: autenticação remota, perfil/administração e homologação nos
seis hosts permanecem rastreados nessas atividades. Exclusão de upload-sessions
não consumidas não está exposta como operação (expiração automática existente).
Nenhuma capacidade pendente foi renomeada como concluída.

A revisão de interface ([ui-review.md](ui-review.md)) alterou apenas a apresentação
e o cliente web; contratos HTTP v1 (30 operações) e MCP (20 tools) não mudaram.
A única rota nova é a página HTML `GET /admin/integrations`, protegida por
`require_admin`, que consome as operações já existentes de F24.

As 20 tools dividem-se em 12 consultas (whoami, capabilities, metrics, list/get
jobs, get_upload, list/get batches, get_export, text, report, artifact) e 8
mutações (prepare_upload, submit_job, create_batch, create_export, cancel_job,
cancel_jobs, retry_job, delete_job_artifacts). Todas compartilham os mesmos
serviços, identidade e schemas; binários trafegam por HTTP autenticado.
