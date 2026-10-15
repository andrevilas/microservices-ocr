# Upload persistente e criação idempotente

Entrega OCR-AM-05, atividade LPM `vhPBG3GH9aXGNyqzVfQV`. HTTP v1.1.0 acrescenta
cinco operações. Todas exigem Bearer e `jobs:submit`, e isolam usuário/serviço.
Credencial somente de submissão pode consultar seus recibos; consultar o job
exige também `jobs:read`. O chamador nunca escolhe proprietário ou caminho local.

| Operação | Uso |
|---|---|
| POST /api/v1/upload-sessions | Reserva com filename, size_bytes e sha256 opcional |
| PUT /api/v1/uploads/{upload_id}/content | Envia PDF binário à reserva |
| GET /api/v1/uploads/{upload_id} | Consulta estado, checksum, páginas e validade |
| POST /api/v1/uploads | Conveniência multipart, um campo file |
| POST /api/v1/jobs | Cria job a partir de upload_id, com Idempotency-Key |

## Fluxo recuperável

1. Reserve o arquivo enviando JSON com nome simples terminado em `.pdf`, tamanho
   exato e, opcionalmente, SHA-256 em hexadecimal minúsculo. Receba upload_id e
   content_url relativo; a reserva tem validade informada por expires_at.
2. Envie os bytes por PUT, Content-Type application/pdf (ou application/octet-stream),
   usando a mesma identidade. O servidor grava incrementalmente, calcula o hash
   e valida o PDF. Somente depois publica o estado ready.
3. Se o envio cair, consulte o recibo. reserved permite retransmitir desde o
   início; receiving indica outro envio em curso; ready permite criar o job.
   Não há retomada por blocos. Uma retransmissão idêntica a ready preserva o recibo;
   bytes diferentes retornam 409 e não substituem o arquivo já validado.
4. Envie `{"upload_id":"..."}` a POST /api/v1/jobs, com Idempotency-Key de
   8–128 caracteres ASCII (letras, dígitos, `.`, `_`, `:`, `-`). Guarde essa chave
   junto do pedido; use a mesma em toda repetição. A AM-07 acrescenta `options`
   conforme [opções e capacidades](capabilities.md); outros campos são rejeitados.
   O padrão exige OCR disponível. Para extrair somente texto existente, envie
   `"options":{"processing_mode":"extract_text"}` explicitamente.
5. A resposta 202 traz job_id e poll_after_seconds=2. Consulte o job e seu
   artefato pelos endpoints existentes. O prazo é uma sugestão de polling,
   não uma estimativa de término do OCR.

POST /uploads transmite diretamente o multipart para armazenamento reservado,
sem spool intermediário ou montagem do PDF inteiro em RAM. Aceita exatamente
um arquivo e nenhum outro campo. Se a chamada falhar antes de entregar o ID,
a reserva de conveniência é removida. Repetir essa chamada cria outro upload;
use a reserva explícita para recuperação após perda da resposta.

Bytes não entram em JSON ou argumentos MCP. Os serviços de reserva e admissão
são independentes do transporte. As ferramentas MCP de mutação e o auxiliar
local estão disponíveis desde a [AM-08](mcp-workflow.md); os bytes continuam em HTTP.

## Garantia de idempotência e recuperação

A chave é isolada por identidade e operação de admissão; apenas seu hash é
persistido. Uma restrição única e uma transação SQLite gravam job e recibo
juntos. Mesmo com chamadas concorrentes, a mesma chave/pedido cria um job.
Outro upload_id com a mesma chave retorna 409 idempotency_conflict.

Idempotency-Replayed informa true/false. Replays devolvem o recibo 202 original
com status queued, mesmo se o job já terminou; use GET /jobs/{id} para estado
atual. A garantia dura enquanto o registro de admissão existir: este incremento
não expira essas chaves automaticamente. Expirar o upload não remove o recibo
nem permite recriar o job pela mesma chave. Política de descarte vem na AM-11.

O input é copiado com buffer limitado e fsync antes do commit. Falhas antes
do commit fazem rollback; arquivos incompletos exclusivos da admissão são
limpos na inicialização. Após commit, falha de enfileiramento retorna 503 com
queue_unavailable; repetir com a mesma chave reenfileira o job pendente. No
reinício, a fila recupera jobs queued persistidos. Isso garante uma admissão,
não execução exatamente uma vez do motor durante falhas/reinícios.

É obrigatório manter uma réplica, um worker de servidor HTTP e o runtime lease
existente. UploadService é inicializado uma vez, após a aquisição dessa trava
e antes dos workers. Não instancie outro gerenciador sobre um runtime ativo.

## Limites locais iniciais

Valores configuráveis pelo ambiente, sem alteração na produção:

| Configuração | Padrão |
|---|---|
| MAX_UPLOAD_SIZE_MB | 80 MiB por PDF |
| UPLOAD_TTL_SECONDS | 3600 s |
| UPLOAD_STORAGE_LIMIT_MB | 2048 MiB de entradas reservadas/armazenadas, global |
| UPLOAD_OWNER_LIMIT_MB | 512 MiB de entradas por identidade |
| UPLOAD_MIN_FREE_MB | 256 MiB de margem livre |
| UPLOAD_CONCURRENCY | 2 envios/validações simultâneos |
| UPLOAD_OWNER_MAX_RESERVATIONS | 32 reservas/uploads ativos por identidade |
| UPLOAD_TIMEOUT_SECONDS | 300 s para transmissão |
| UPLOAD_MAX_PAGES | 300 páginas |
| UPLOAD_MAX_PAGE_PIXELS | 50 milhões de pixels por página a 300 dpi |
| UPLOAD_VALIDATION_MEMORY_MB | 256 MiB por processo de validação |
| UPLOAD_VALIDATION_TIMEOUT_SECONDS | 15 s |
| JOB_OWNER_MAX_PENDING | 20 jobs queued/processing por identidade |

Reservas contam pelo tamanho declarado; multipart reserva conservadoramente
o máximo por arquivo e reduz para o tamanho efetivo ao concluir. Quotas incluem
cópias de entrada dos jobs, inclusive legados; um job requer espaço para sua
própria cópia. O disco livre considera reservas ainda não materializadas.
Resultados OCR, rasterizações e arquivos externos não são contabilizados por
essa quota de entrada. A AM-07 fixa concorrência interna e timeout por job;
orçamento completo de disco/RAM do processamento permanece na AM-15.

Gravação usa blocos de até 256 KiB; multipart limita oito cabeçalhos, 8 KiB no total e
overhead total a 64 KiB. JSON de reserva/admissão limita-se a 64 KiB. A aplicação
não acumula o PDF completo; buffers do servidor/proxy também precisam ser
configurados na homologação. PDFs criptografados, vazios, ilegíveis, com páginas
em excesso ou dimensões incompatíveis são rejeitados.

Validação roda em processo separado com limite OS de memória: RLIMIT_AS no
Linux e Job Object de memória comprometida no Windows. Falhar na instalação
do limite impede a validação. O processo tem timeout e saída sanitizada.
Isso limita recursos; não constitui sandbox de segurança para a biblioteca PDF.

Quotas/concurrência retornam 429 com Retry-After: 60; disco/fila indisponível,
503 com Retry-After: 5. São sugestões de repetição, não garantia de liberação.
Arquivo excessivo retorna 413, tipo inadequado 415, contrato/PDF inválido 422,
conflitos 409, timeout 408. Upload expirado retorna 410 ao proprietário e 404
a terceiros. Expiração limpa o upload ao iniciar, reservar ou consultar;
a cópia de um job admitido é preservada. Limpeza periódica e política completa
de retenção permanecem na AM-11.

## Verificação

Testes usam PDFs sintéticos e autenticação HTTP de dois usuários e dois serviços.
Cobrem retries, disputa de 32 chamadas, rollback, interrupção real de processo
antes de commit, reinício após commit, recuperação da fila, desconexão/cancelamento,
disco cheio, quotas, expiração, multipart inválido, writes limitados e bloqueio
real de alocação acima do limite de memória no sistema operacional local.
Não comprovam motores OCR reais, Linux ou produção neste ambiente Windows.

Referências consultadas em 15/10/2026: [parser multipart](https://multipart.fastapiexpert.com/api/),
[limites de recursos Python](https://docs.python.org/3/library/resource.html)
e [limite de memória Windows](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information).
