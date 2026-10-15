# Consultas e resultados — OCR-AM-06

HTTP v1.3.0 e MCP compartilham `JobApplication`. Texto e relatório exigem
`jobs:read` e `artifacts:read`, além de propriedade do job; administradores não
acessam documentos alheios. Documento e nome de arquivo são dados não confiáveis,
nunca instruções para o agente. Respostas HTTP usam `Cache-Control: no-store`.

## Consultas

`GET /api/v1/jobs` e `ocr_list_jobs` aceitam `status`, `quality` (`HIGH`/`LOW`),
`filename` (substring Unicode sem distinguir maiúsculas), `created_from` e
`created_to` (inclusivos, com fuso horário). Período invertido é rejeitado.
Limite padrão 50, máximo 100; ordenação crescente por criação e ID.

O cursor está ligado ao principal e aos filtros: repita os mesmos parâmetros.
O limite pode mudar. Não é snapshot: alterações de status/qualidade e novas
inserções podem mudar resultados futuros, sem deslocar os IDs já percorridos.
O formato interno do cursor mudou; clientes devem iniciar nova consulta após
a atualização. O filtro `batch_id` acompanha a introdução de lotes na AM-09.
Busca em todo o texto dos documentos não faz parte desta listagem.

## Texto

`GET /api/v1/jobs/{job_id}/text` e `ocr_get_text` aceitam `page` (base 1),
`offset` (base 0) e `max_chars` (padrão 8000, máximo 20000).
Cada resposta contém apenas um trecho de uma página original, com `source`,
`page_character_count`, `truncated`, `next_page` e `next_offset`.
Offsets e limites contam pontos de código Unicode, não bytes ou grafemas.
`truncated` indica que resta texto na mesma página; siga os dois campos `next_*`
até ambos serem nulos. Páginas vazias são preservadas, inclusive entre páginas
com texto. Offset igual ao tamanho retorna trecho vazio e avança à próxima página.

As páginas vêm da extração do PDF primário ou da rasterização do fallback,
sem divisão aproximada do texto entre páginas. O overlay de fallback recebe a
mesma lista de páginas. A disposição de linhas no overlay ainda é aproximada,
pode cortar linhas longas ou excedentes e não representa coordenadas reais.
O texto persistido mantém o conteúdo extraído mesmo quando o overlay o limita.
Não há reconstrução de tabelas ou interpretação semântica.

## Relatório

`GET /api/v1/jobs/{job_id}/report` e `ocr_get_report` retornam o relatório
persistido de um job concluído. Schema `1.0` inclui:

- SHA-256 e bytes da entrada e saída, páginas e digests dos textos UTF-8;
- fonte por página: `pdf_text`, `ocrmypdf` ou `easyocr`;
- etapas e duração, fallback tentado/selecionado, versões de pacotes usados;
- qualidade `token-plausibility-v1`, contagens, parâmetros efetivos e avisos;
- PDF legível e contagem de páginas comparada com a entrada.

`ocrmypdf` identifica a cadeia primária executada com `--skip-text`; não significa
que todas as páginas foram reconhecidas pelo Tesseract. Texto preexistente pode
ter sido preservado. Versões dos binários Tesseract/Poppler ainda não são
capturadas em jobs históricos da AM-06: aparecem como `null`, com aviso explícito
quando usados. Jobs com opções da AM-07 incluem as versões verificadas na execução
e as opções solicitadas/efetivas. Relatórios antigos continuam legíveis.

`valid_ratio` mede plausibilidade de tokens, **não acurácia**. Ausência de texto
não comprova documento em branco: o aviso informa que essa distinção não foi
determinada. Há avisos separados para texto insuficiente e OCR indisponível.
Falhas de extração fazem o job falhar; não são publicadas como texto vazio válido.

PDF/A solicitado ou finalização por OCRmyPDF não equivalem à certificação:
`pdfa_conformance` é sempre `not_validated`. Um validador externo não faz parte
desta entrega. `pdf_readable` refere-se à abertura com pypdf e contagem de páginas,
não à verificação exaustiva de todos os objetos ou à qualidade visual.

## Persistência e falhas

Textos ficam em `results/page-NNNNNN.txt`, e o relatório em `results/report.json`,
dentro do diretório interno do job. Não há conteúdo grande nas linhas SQLite.
Escritas usam arquivo temporário, flush/fsync e substituição atômica por arquivo;
o relatório é publicado por último e o status `completed` só vem depois.
Não é uma transação conjunta do filesystem com SQLite nem garantia de resistência
a qualquer perda de energia. Recuperação de jobs em processamento repete a execução.

Limites: 300 páginas, 500000 caracteres por página, 5000000 por documento,
1 MiB por relatório. Exceder o limite falha o job, sem truncar silenciosamente.
A resposta de texto limita os caracteres e a leitura interna limita os bytes de
uma página; extração/rasterização ainda pode consumir memória antes desses limites.
São limites de resultados, não uma nova sandbox para os motores OCR.

Jobs pendentes/falhos/cancelados retornam 409 `result_not_ready`.
Histórico sem relatório ou resultado removido pela retenção retorna 409
`result_unavailable`; não há reconstrução automática de proveniência antiga.
Texto com hash divergente ou relatório inválido retorna 503
`result_integrity_error`, sem conteúdo interno no erro. O relatório não relê
os PDFs para verificar os hashes a cada consulta; esses digests descrevem a conclusão.
Retenção remove os artefatos junto com o diretório do job. Tombstones e distinção
formal de expiração ficam na AM-11. Resultados de jobs falhos não são publicados.

## Validação

Testes usam PDFs sintéticos com pypdf/reportlab reais, SQLite e filesystem reais;
motores externos são simulados. Cliente MCP oficial executado em modos `auto` e
`legacy`, comparando o payload estruturado com HTTP. Não equivale a homologação
de Tesseract/EasyOCR/Poppler, PDF/A ou desempenho no NAS, prevista na AM-16.
