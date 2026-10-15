# Opções e capacidades — OCR-AM-07

Contrato HTTP 1.4.0. `GET /api/v1/capabilities` e `ocr_get_capabilities` exigem
Bearer válido, sem escopo adicional. Não retornam documentos, credenciais,
caminhos de instalação ou stderr. O catálogo informa motores/versões, motivos
de indisponibilidade, idiomas suportados instalados, perfis, defaults e limites.

## Opções aceitas

`POST /api/v1/jobs` aceita `upload_id` e `options`. Apenas os campos abaixo
são permitidos. Não se aceitam parâmetros de linha de comando, plugins, caminhos,
URLs ou opções de shell.

| Campo | Valores | Padrão para OCR |
|---|---|---|
| processing_mode | preserve_existing_text, extract_text | preserve_existing_text |
| languages | eng, por; sem duplicatas | [eng] |
| output_format | pdf_searchable, pdfa | pdfa |
| compression_profile | none, balanced | balanced |
| fallback_policy | disabled, if_low_quality | disabled |

`preserve_existing_text` usa `--skip-text` e especifica os idiomas escolhidos.
Não refaz silenciosamente OCR existente. `balanced` usa otimização nível 1;
`none`, nível 0. PDF/A ainda envolve conversão: nenhuma das opções garante
identidade visual/byte a byte ou redução do tamanho. Conformidade PDF/A continua
`not_validated` no relatório.

`extract_text` copia o PDF e extrai somente o texto que já existe. Nesse modo,
campos omitidos resolvem para idiomas vazios, pdf_searchable, none e disabled;
outras combinações são rejeitadas. O nome do formato identifica o tipo de saída,
não promete que um scan sem texto passe a ser pesquisável.

Fallback exige o par `[eng, por]`, Tesseract com ambos os idiomas e EasyOCR local
pronto para `[pt, en]`. A escolha `if_low_quality` conserva o critério heurístico
existente para decidir se o resultado do fallback substitui o primário.
O padrão `disabled` evita ativação implícita. Não são oferecidos force/redo OCR,
idiomas arbitrários, compactação agressiva ou download automático de modelos.

```json
{
  "upload_id": "00000000000000000000000000000001",
  "options": {
    "languages": ["por"],
    "processing_mode": "preserve_existing_text",
    "output_format": "pdf_searchable",
    "compression_profile": "none",
    "fallback_policy": "disabled"
  }
}
```

O catálogo lista valores disponíveis, mas nem todo produto cartesiano é válido:
as restrições dos modos e do par de idiomas acima também se aplicam. Os defaults
são estáveis e podem não estar disponíveis no host; não são automaticamente
substituídos por extração de texto. OCR solicitado sem dependências → 503
`ocr_unavailable`; idioma ausente → 422 `language_unavailable`; fallback indisponível
→ 503 `fallback_unavailable`; combinação inválida → 422 `unsupported_option_combination`.

## Detecção e limites

As sondagens executam comandos fixos de versão/idiomas com timeout de 5 segundos.
OCRmyPDF precisa importar/executar o CLI da versão 16.4–16.x; Tesseract >=4.1.1
(exceto 5.4.0) e Ghostscript >=9.54 (exceto 10.0.0 até 10.02.0) são exigidos.
Somente eng/por encontrados em `tesseract --list-langs` entram no catálogo.
Poppler exige `pdfinfo` e `pdftoppm` executáveis. Importação de pacote isolada
não basta para anunciar OCR disponível.

EasyOCR é verificado em subprocesso separado, com timeout de 20 segundos,
carregando Reader CPU para pt/en com `download_enabled=False`. Isso verifica
dependências e modelos locais, pode consumir RAM e criar diretórios de cache,
mas não baixa pesos. Tempo excedido marca o fallback indisponível. A execução
normal também desabilita downloads; provisionamento de pesos é responsabilidade
operacional, não efeito de uma chamada de descoberta ou de um documento.

Catálogo tem cache de 60 segundos, protegido por lock contra sondagens
simultâneas; cada cliente recebe cópia independente. O primeiro acesso pode
demorar até cerca de 50 segundos somando sondagens. Valores de limites são lidos
novamente em cada consulta. O worker revalida dependências sem usar o cache antes
de executar um job com opções. Revalidação não elimina corridas com alterações
externas no host nem substitui teste OCR real; falhas posteriores ainda falham o job.

Limites divulgados incluem bytes/páginas/pixels do upload, quotas de entrada,
uploads simultâneos, jobs pendentes, workers, timeout e tamanho do texto.
Páginas são limitadas ao menor valor entre configuração e 300.
`OCR_ENGINE_JOBS` (padrão 1; 1–8) limita os workers internos do OCRmyPDF por job.
O timeout por subprocesso é congelado na admissão. Isso não é uma quota completa
de RAM/disco do processamento: rasterização/fallback ainda não têm sandbox ou
deadline global. A AM-15 continua responsável pelos controles restantes.

## Persistência e compatibilidade

Migração SQLite aditiva cria options_requested, options_effective e error_code.
Os modelos de opções são imutáveis; valores efetivos incluem paralelismo, timeout,
limiares da qualidade e tolerância do fallback, congelados por job. Engines
recebem argumentos tipados; settings globais não são modificados. Consulta do job
e relatório mostram os dois conjuntos de opções. Relatórios antigos usam null.

Idempotência inclui as opções normalizadas: ordem dos idiomas e defaults
omitidos/explícitos não criam novas requisições. Alterar opções com a mesma chave
retorna 409, e repetir a mesma requisição devolve o recibo original mesmo se o
motor ficou indisponível. Recibos anteriores à AM-07 continuam reproduzíveis
quando o cliente omite opções; não se inventam opções históricas.

Jobs antigos/legados com opções null mantêm a execução anterior, inclusive sua
possibilidade de copiar o PDF na ausência de OCR. Esta mudança não migra o painel
legado (AM-12). Jobs HTTP v1 novos sem options passam a exigir OCR disponível;
clientes que querem apenas extração precisam solicitá-la explicitamente.
No worker, indisponibilidade gera estado failed e error_code estável. Não há
downgrade silencioso para extração. O Dockerfile passa a incluir eng e por,
mas alterar o arquivo não instala nada na instância atual nem prova o build.

## Fontes técnicas e validação

Compatibilidade de versões segue as verificações do OCRmyPDF 16.10.4:
[Tesseract](https://github.com/ocrmypdf/OCRmyPDF/blob/v16.10.4/src/ocrmypdf/builtin_plugins/tesseract_ocr.py)
e [Ghostscript](https://github.com/ocrmypdf/OCRmyPDF/blob/v16.10.4/src/ocrmypdf/builtin_plugins/ghostscript.py).
O controle de modelos locais usa o parâmetro documentado de
[EasyOCR Reader](https://www.jaided.ai/easyocr/documentation/).

Testes verificam contratos e erros com sondagens simuladas, preservação das opções,
concorrência, extração de PDF sintético, execução simulada dos motores e paridade
com o cliente MCP oficial. Há sondagem real do ambiente local, sem instalação.
Homologação dos motores, Linux, português, PDF/A e desempenho no NAS permanece
na AM-16. A implementação foi revisada pelo próprio agente, sem revisão independente.
