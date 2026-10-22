# Lotes e exportação assíncrona

Entrega OCR-AM-09, LPM `B6WE9dwgmEvlxm9JzbCO`. Contrato atual: HTTP 1.12.0
(77 operações) e MCP 0.10.0 (62 ferramentas: 39 leituras, 23 escritas). As 20
ferramentas do subconjunto OCR legado (12 leituras e oito mutações) incluem o
[controle de jobs da AM-10](job-control.md). As 12 operações documentais estão em
[metadados documentais](document-metadata-api.md); veja o
[contrato HTTP](http-contract.md) e [API/MCP](api-mcp.md) mais recentes.

| HTTP | MCP | Escopos |
|---|---|---|
| POST /api/v1/batches | ocr_create_batch | jobs:submit |
| GET /api/v1/batches | ocr_list_batches | jobs:read |
| GET /api/v1/batches/{id} | ocr_get_batch | jobs:read |
| POST /api/v1/exports | ocr_create_export | jobs:read e artifacts:read |
| GET /api/v1/exports/{id} | ocr_get_export | jobs:read e artifacts:read |
| GET /api/v1/exports/{id}/content | URL autenticada do recibo | jobs:read e artifacts:read |

Os mesmos contratos estão no MCP HTTP e no cliente stdio. Bytes não trafegam
em ferramentas MCP. Use Bearer da mesma identidade no upload e nos downloads.

## Admissão de lote

Prepare/transmita os uploads seguindo [uploads.md](uploads.md). Então envie:

```json
{
  "items": [
    {"client_item_id": "documento-a", "upload_id": "00000000000000000000000000000001", "options": {"processing_mode": "extract_text"}},
    {"client_item_id": "documento-b", "upload_id": "00000000000000000000000000000002", "options": {"processing_mode": "extract_text"}}
  ]
}
```

HTTP exige `Idempotency-Key`; MCP recebe `idempotency_key`. A chave aceita
8–128 caracteres ASCII dentre letras, dígitos, `.`, `_`, `:` e `-`.
`client_item_id` é único dentro do lote, tem até 80 desses caracteres e preserva
a correlação com o sistema cliente. O mesmo upload pode aparecer mais de uma
vez com IDs de cliente diferentes, inclusive para opções distintas.

Admissão `all_or_nothing`: todos os uploads devem existir, estar prontos, não
expirados e pertencer ao solicitante. As opções precisam ser aceitas pelas
capacidades atuais. Validação, limite ou falha na persistência não deixa um
subconjunto de jobs. A transação grava todos os jobs e o manifesto do lote;
a fila recebe os IDs após o commit. Falha de despacho retorna `queue_unavailable`:
repita a mesma chave/payload. O reinício também recupera jobs pendentes.

HTTP 202/MCP devolve `batch_id`, `created_at`, `admission_policy`, `items` e
`poll_after_seconds`. O recibo de admissão conserva os estados iniciais, mesmo
quando repetido após conclusão. As chaves são separadas por principal e operação;
ordem, IDs de cliente, uploads ou opções diferentes com a mesma chave conflitam.

Consulte o lote para os estados atuais, contadores, erros por item e progresso
médio. `terminal=true` significa que todos terminaram, inclusive possíveis falhas
ou cancelamentos; não significa sucesso de todos. O processamento é independente
depois da admissão. A listagem usa ordem de criação/ID e cursor vinculado ao dono.
`GET /jobs?batch_id=...` e `ocr_list_jobs(batch_id=...)` filtram o histórico; o
cursor também se vincula a esse filtro. Lote alheio e inexistente retornam 404.

## Exportação

Envie `{"job_ids": ["...", "..."]}` e uma chave de idempotência. São aceitos
até 25 IDs distintos e explícitos. Omissão de identidade, escopo ausente e IDs
alheios/inexistentes são rejeitados antes de persistir a solicitação.

HTTP 202/`ocr_create_export` devolve imediatamente um recibo `queued`, com
`export_id` e intervalo de consulta. Um worker de exportação da mesma instância
prepara o ZIP em disco. Consulte `ocr_get_export`/GET até `ready` ou `failed`.
Fechar o cliente não cancela a exportação. O banco mantém a solicitação;
na inicialização, `processing` volta a `queued` e a preparação é retomada.

O manifesto reflete os jobs quando o worker monta o ZIP, não uma promessa de que
todos os jobs estarão concluídos no instante da admissão. Inclui, para cada ID:
nome original, estado, inclusão/exclusão, nome no ZIP, tamanho e SHA-256 quando
incluído. Motivos de exclusão são `job_not_completed` e `artifact_unavailable`.
Se nenhum resultado estiver disponível, o ZIP contém apenas `manifest.json`.
Isso permite inspecionar todas as exclusões sem omissão silenciosa.

Os nomes físicos são `001-<job_id>.pdf`, `002-<job_id>.pdf`, etc.; nomes originais
repetidos ou contendo caracteres especiais não colidem nem viram caminhos.
O ZIP contém PDFs e manifesto; texto por página/relatórios continuam nos
endpoints da AM-06. A escrita usa blocos de 256 KiB e `ZIP_STORED`, sem manter
o ZIP completo em RAM. Essa modalidade armazena membros sem recompressão,
conforme a [documentação do Python](https://docs.python.org/3/library/zipfile.html).

Em `ready`, o recibo informa SHA-256/tamanho do ZIP, `download_url` e `expires_at`.
Faça download HTTP autenticado e confira o hash. Download antes de `ready`
retorna 409; expiração retorna 410. Download de ZIP é integral, sem promessa de
Range. Uma transferência já aberta mantém uma referência ao arquivo até terminar
ou desconectar; a limpeza não apaga seu arquivo durante a transferência.

Repetir a chave/payload devolve o recibo original `queued`; consulte o estado
atual pelo ID. Mudança de IDs ou de ordem conflita. Depois da expiração, a chave
antiga não recria o ZIP: solicite um novo snapshot com chave nova. Uma exportação
`failed` tem `error_code` sanitizado e pode ser novamente solicitada com outra chave
depois de corrigida a causa. Não há retries automáticos infinitos de exportações
que falharam. Registros e chaves persistem sem TTL. Um job incluído cujo conteúdo
foi removido/expirado invalida novas leituras do ZIP com 410 `export_source_removed`;
transferências já abertas terminam. Veja [retenção](retention.md).

## Limites e execução

| Configuração | Padrão |
|---|---:|
| Itens por lote | min(MAX_BATCH_FILES, 25) |
| BATCH_MAX_TOTAL_MB | 256 MiB de entradas |
| JOB_OWNER_MAX_PENDING | 20 jobs, incluindo todo o novo lote |
| EXPORT_MAX_TOTAL_MB | 256 MiB de PDFs por ZIP, mais até 64 KiB de metadados |
| EXPORT_STORAGE_LIMIT_MB | 2048 MiB de ZIPs |
| EXPORT_OWNER_LIMIT_MB | 512 MiB de ZIPs por dono |
| EXPORT_OWNER_MAX_PENDING | 4 exportações por dono |
| EXPORT_MAX_PENDING | 32 exportações na instância |
| EXPORT_TTL_SECONDS | 3600 s a partir do início da montagem |

Capacidades anunciam os limites de lote/exportação e filas. Quotas de uploads,
reservas e espaço livre continuam aplicáveis. Os limites do ZIP são revalidados
pelo worker, pois os resultados podem mudar entre admissão e preparação; uma
solicitação aceita pode falhar com um código de quota/tamanho/armazenamento.

Somente um processo detém a trava do diretório; não execute múltiplos workers
Uvicorn/réplicas sobre o mesmo SQLite. O worker de exportação é único por runtime.
A cópia do snapshot segura a trava do store para não disputar com limpeza;
exportações grandes podem atrasar consultas/atualizações, limitadas pelo tamanho
configurado. Não há benchmark do NAS nesta entrega. ZIPs expirados ou invalidados
sem downloads ativos são removidos no startup, na verificação de capacidade e
pelo coletor periódico da AM-11. Arquivos incompletos/orfãos de exportação
são reconciliados no startup sob a trava de runtime.

## Compatibilidade legada

`POST /api/jobs/batch` mantém multipart e opções legadas. Valida todos os PDFs
antes da transação, guardando temporariamente um arquivo por vez em disco.
Retorna os campos aditivos `batch_id` e `dispatch_pending`; esse último indica
que ao menos um encaminhamento falhou, preservando todos os IDs para consulta e
recuperação. O legado não ganha idempotência de requisição; novos clientes devem
usar uploads por referência e API v1.

`GET /api/jobs/download-batch` passa a escrever em arquivo temporário e acrescenta
`manifest.json`. Preserva nomes simples quando únicos e diferencia duplicados.
Continua omitindo IDs não visíveis para preservar a semântica legada, sem revelá-los
no manifesto. A API v1 rejeita o conjunto se qualquer ID for alheio/inexistente.
O download legado é transitório, sem export_id/TTL; a API v1 é o caminho persistente.
Há no máximo dois ZIPs legados simultâneos, incluindo transferências abertas;
o excedente recebe 429. O espaço livre mínimo é verificado antes de montar o arquivo.

## Testes e limites da evidência

`tests/test_bulk.py` cobre lote misto, rollback na segunda gravação, concorrência,
replay/reabertura, falha independente de item, escopos, cursor/filtro por lote,
ZIP com nomes repetidos, hashes/manifestos, limites, falha de geração, expiração,
download interrompido e regressão legada. Clientes oficiais MCP HTTP/stdio em
`auto`/`legacy` percorrem lote → processamento → exportação → download real.
Há ensaio de morte do processo durante preparação de exportação e recuperação
do mesmo ID/recibo depois do reinício.

São PDFs sintéticos com texto digital, em ambiente Windows local. Não comprovam
OCR de imagem, conformidade PDF/A, Linux/container, carga do NAS ou produção.
