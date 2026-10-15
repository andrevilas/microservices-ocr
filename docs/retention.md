# Retenção, expiração e exclusão

Entrega OCR-AM-11, LPM `IgIXxmrsydwTZX9hQ9wT`. Contrato HTTP 1.6.0:
30 operações; MCP 0.5.0: 20 ferramentas (12 leituras, oito mutações).

`DELETE /api/v1/jobs/{job_id}/artifacts` e `ocr_delete_job_artifacts`
exigem `artifacts:delete` e propriedade do job. Esse escopo é independente
de leitura/submissão e deve ser concedido explicitamente a credenciais de
serviço. Tokens existentes não ganham a permissão. Usuários humanos podem
remover os próprios arquivos. Administradores também ficam limitados aos
próprios documentos; IDs alheios e inexistentes retornam o mesmo 404.

## Prazo e estados

O prazo é gravado ao entrar em completed, failed ou canceled:
`artifacts_expires_at = updated_at + JOB_RETENTION_SECONDS`. Jobs ativos,
incluindo cancel_requested, não são coletados. Consultas, atualizações de
metadados e mudanças posteriores da configuração não renovam esse prazo.
Na migração, registros terminais antigos recebem uma única vez o prazo a
partir do updated_at histórico e da configuração em vigor. Portanto, registros
antigos podem expirar já no primeiro startup; preparar backup e validar essa
política antes de uma futura implantação.

Jobs e metadados de artefatos incluem `retention`:

| Campo | Significado |
|---|---|
| state | active, retained, expired ou deleted; independente do status de processamento |
| expires_at | Prazo fixo; null enquanto o job não terminou |
| unavailable_at | Prazo vencido antes da coleta, ou instante em que o tombstone foi gravado |
| purged_at | Confirmação persistida de remoção do diretório do job |
| purge_pending | Acesso encerrado, mas remoção física ainda não confirmada |

`GET /jobs/{id}` e `GET /jobs/{id}/artifact` continuam retornando 200 ao dono,
com estado de retenção e sem URL disponível. Conteúdo PDF, texto e relatório
passam a retornar 410 `artifacts_expired` ou `artifacts_deleted`, inclusive
antes da próxima coleta. Resultado nunca produzido/registrado retorna 409.
O download legado também respeita a retenção. Cada novo pedido Range é uma
nova leitura: um download retomado após a expiração recebe 410.

## Exclusão e concorrência

A exclusão só aceita jobs terminais; queued, processing e cancel_requested
retornam 409 `job_not_terminal`. A resposta 200 é um recibo com job_id e
retention. Não exige Idempotency-Key. Repetir preserva motivo e instante da
remoção, sem novos eventos de auditoria. `purged_at` e `purge_pending` podem
mudar quando uma remoção adiada termina. Excluir um job já expirado mantém
o motivo expired.

Primeiro, a mesma transação grava tombstone e evento de auditoria no SQLite.
Só então bloqueia novas leituras e tenta remover o diretório. Falha nessa
transação preserva arquivos e cache. Falha física ou na confirmação do purge
mantém o tombstone e permite nova tentativa periódica. A auditoria guarda
ID, evento, principal, motivo e instante; não inclui nome ou conteúdo do PDF.

PDFs (integrais e Range), texto e relatório obtêm uma referência de leitura
antes de acessar arquivos. Exclusão ou expiração durante essa leitura marca
o job indisponível, mas preserva o diretório até a última referência terminar.
Conclusão, desconexão e cancelamento da transferência liberam a referência.
O envio usa os chunks do FileResponse; a extensão pathsend é desativada para
não liberar a referência antes de o servidor consumir o caminho.

Todos os arquivos daquele job são removidos: entrada copiada, saída, texto,
relatório e intermediários. Outros jobs/retries e uploads independentes têm
seus próprios prazos. Um retry novo do original removido recebe 410; um replay
de retry já admitido continua devolvendo o recibo persistido.

Um ZIP pronto que inclua um job removido/expirado deixa de aceitar novas
leituras imediatamente (`export_source_removed`, 410). Downloads de ZIP já
abertos terminam; o coletor remove o ZIP quando não há referências ativas.
Novo snapshot com chave nova pode ser criado com exclusões explícitas no
manifesto. ZIPs apenas com itens excluídos continuam válidos até seu TTL.
`purged_at` do job confirma apenas seu diretório; cópias em ZIP são coletadas
separadamente. O ZIP legado temporário já admitido termina e fecha seu arquivo.

## Coleta e persistência

| Configuração do operador | Padrão |
|---|---:|
| JOB_RETENTION_SECONDS | 86400 s após o job terminar |
| RETENTION_INTERVAL_SECONDS | 60 s; aceito de 1 a 3600 |
| UPLOAD_TTL_SECONDS | 3600 s desde a reserva |
| EXPORT_TTL_SECONDS | 3600 s desde o início da montagem |

Capacidades anunciam os limites. A configuração é do operador; opções de job
não permitem prolongar retenção. Um único coletor pertence ao lifespan sob
a trava exclusiva do runtime: executa no startup e periodicamente, sem precisar
de nova requisição ou reinício. Downloads e uploads em recepção são respeitados.
Falha em uma categoria não impede a tentativa das demais. Erros são registrados
sem caminhos ou conteúdo; não há promessa de prazo máximo de purge se o disco
estiver bloqueado ou indisponível.

No startup, temporários de uploads/exportação e diretórios legacy-batch com o
padrão exato da aplicação são reconciliados antes de aceitar requisições.
Diretórios desconhecidos são preservados. Remoção recursiva de jobs/staging
confere destino absoluto resolvido e rejeita symlinks/junctions, inclusive
internos. O diretório de dados deve permanecer privado ao processo: essas
verificações não são uma sandbox contra alterações concorrentes de um usuário
local malicioso. Nenhuma varredura percorre pastas externas.

Metadados de jobs, lotes, uploads e exportações, chaves de idempotência e
auditoria permanecem sem TTL nesta versão, para preservar 410, rastreabilidade
e replay. Não confundir exclusão de arquivos com apagamento de histórico ou
anonimização; nomes e hashes já presentes nesses registros permanecem. Não há
apagamento seguro de blocos nem remoção de cópias já baixadas ou backups.
Excluir uma sessão de upload por API/MCP ainda não está disponível; seu TTL
é aplicado periodicamente. O endpoint desta entrega é de artefatos do job.

## Validação

`tests/test_retention.py` exercita persistência e migração, escopos, 404/409/410,
idempotência, coleta real periódica, downloads integrais/Range, desconexão,
leituras concorrentes, falhas de disco/transação, retomada de purge, ZIPs e
proteção de diretórios externos/junctions. Clientes oficiais MCP por HTTP e
stdio, em modos auto/legacy, verificam exclusão, paridade HTTP e reinício.
Dados sintéticos e filesystem/SQLite locais no Windows; não comprova Linux,
qualidade OCR, desempenho do NAS ou migração em produção.
