# PDF/A e compactação (OCR-EV-01B / E1)

`app/services/pdfa_validator.py` encapsula o veraPDF como executável externo com comando fixo no servidor; requisições escolhem apenas o arquivo.

## Contrato de plataforma

- A execução do veraPDF é **somente POSIX** (produção: Linux/Docker). No Windows, `validate()` retorna `indeterminate` com `unsupported_platform` **antes** de iniciar qualquer processo, e a capacidade `engines.verapdf` aparece indisponível com esse motivo. As demais operações continuam funcionando no Windows; trabalhos `pdfa` falham de forma fechada.
- Testes de execução real de processo (timeout, limite de relatório, descendentes) rodam apenas em POSIX; os testes do parser usam saída injetada em processo e rodam em todas as plataformas.

## Configuração (somente servidor)

- `VERAPDF_EXECUTABLE`: caminho do executável (sem parsing de shell; nunca vem da requisição). Ausente → motivo `validator_not_configured`.
- `VERAPDF_TIMEOUT_SECONDS` (padrão 120, 1–3600), registrado em `options_effective.pdfa_validation_timeout_seconds`.
- Docker: copia apenas `/opt/verapdf` da imagem oficial `verapdf/cli` 1.30.2 fixada por digest; o Java vem do pacote Debian nativo `openjdk-21-jre-headless`. O build executa `verapdf --version` (`RUN`) e passou. Não há socket Docker montado nem serviço validador exposto.

## Validação final

- Para `output_format=pdfa`, após conversão/compressão o orquestrador valida o arquivo final como PDF/A-2b. O relatório (`output_validation`) registra `pdfa_conformance` (`passed`/`failed`/`indeterminate`), perfil, validador e versão, hash validado, códigos de motivo e estatísticas.
- Só `passed` conclui o trabalho. `failed` ou `indeterminate` persistem o relatório e então marcam o trabalho como `failed` (`pdfa_validation_failed` / `pdfa_validation_indeterminate`).
- `pdf_searchable` e `extract_text` não são validados nem declarados PDF/A (`not_validated`, aviso `pdfa_conformance_not_validated`).
- Trabalhos legados (sem opções) usam `FINAL_OUTPUT_TYPE`; com o padrão `pdfa` a saída é PDF/A e **é validada** como qualquer trabalho `pdfa`. Só `FINAL_OUTPUT_TYPE=pdf` produz `pdf_searchable` (`not_validated`).
- `extract_text` com `pdfa` é recusado (422) na admissão e novamente na finalização; uma cópia nunca é rotulada como PDF/A.
- Sem validador disponível, a admissão da API recusa `output_format=pdfa` e a capacidade não lista `pdfa`; `pdf_searchable` continua possível.
- Trabalhos `failed` com relatório retido expõem o relatório de diagnóstico sob a mesma ACL dos resultados; texto e PDF não são publicados. Sem relatório registrado, a consulta retorna `409 result_unavailable`; após expiração ou exclusão o acesso é encerrado.
- Validação: cópia privada congelada; `sha256` cobre os bytes validados; divergência da origem → `input_changed_during_validation`. Relatório ausente, incompleto ou contraditório → `indeterminate` (regras de `batchSummary`, `details` e contadores ASCII inalterados).

## Presets (`compression_preset_version` = `2026-10-16.1`)

OCRmyPDF 16.x com `--output-type pdfa-2` explícito para PDF/A (`pdf` para PDF pesquisável), sempre `--skip-text`. Nunca JBIG2 com perdas, `--force-ocr`, rasterização ou reamostragem/upscaling.

| Preset | Argumentos | Observação |
| --- | --- | --- |
| `none` | `--optimize 0` | compatível com versões anteriores |
| `balanced` (padrão) | `--optimize 1` | otimizador sem perdas, mas a conversão PDF/A do Ghostscript pode recodificar JPEG: **não é totalmente sem perdas** |
| `economic` | `--optimize 2 --jpeg-quality 75` | **experimental** até benchmark; sem `pngquant`, cai para `--optimize 1` e registra `pngquant_unavailable_used_optimize_1` |
| `preservation` | `--optimize 0` + `--pdfa-image-compression lossless` (só PDF/A) | não promete identidade de bytes da derivada |

Os argumentos efetivos e o fallback ficam em `output_validation.compression_arguments`/`compression_fallback`. Nenhum preset altera a resolução nem garante 300 dpi.

## UI

Os padrões seguem a API (`capabilities.defaults`, inclusive idiomas, `pdfa`/`balanced`) quando oferecidos pelas capacidades; os seletores listam apenas valores reais. Se o validador estiver indisponível, `pdfa` não é oferecido nem selecionado (usa-se `pdf_searchable`) e a nota de capacidades exibe o motivo. Quando oferecido, o rótulo é neutro ("PDF/A-2b (validação obrigatória)") e não promete aprovação. Um trabalho `failed` retido mostra apenas o relatório (conformidade e motivos), sem texto nem download. Cobertura: `tests/test_pdfa_ui.py` (respostas HTTP simuladas).

## Evidência real (2026-10-16)

Executada pelo Codex em Docker Linux, imagem `sha256:c04d926e9a455cc47a198067c417e5664501e9dd668b1506622041a1a67f0a28`, com documentos sintéticos (nenhum dado original):

- 9 conversões (3 presets × born-digital, digitalizado 300 dpi e misto) validadas pelo veraPDF como PDF/A-2b.
- Harness do produto: 8 casos aprovados — 6 (presets × digital/misto) e 2 negativos (finalizador que omite a conversão e validador ausente), ambos falhando de forma fechada.
- Digitalizado de 1.044.861 bytes → `balanced` 703.586, `economic` 429.811, `preservation` 843.583.
- CER/WER 0 em um corpus pequeno e idealizado; **não** é medida de precisão universal.

## Limites

- Um descendente que crie a própria sessão escapa do `killpg`.
- A derivada não preserva assinaturas digitais do original; `passed` não declara isso.
- Testes unitários usam veraPDF/OCRmyPDF falsos e só comprovam regras do wrapper e da integração. Testes com engines simuladas que não tratam de PDF/A fixam `FINAL_OUTPUT_TYPE=pdf` localmente; os testes PDF/A explícitos ficam em `tests/test_pdfa_integration.py`.
- A evidência real acima não constitui benchmark completo nem homologação de produção. O preset `economic` continua **experimental**. Não há homologação no TrueNAS nem com limite de 2 GB de memória (o harness usou 4 GB).
