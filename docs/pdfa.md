# PDF/A e compactação (OCR-EV-01B / E1)

`app/services/pdfa_validator.py` encapsula o veraPDF como executável externo com comando fixo no servidor; requisições escolhem apenas o arquivo.

## Contrato de plataforma

- A execução do veraPDF é **somente POSIX** (produção: Linux/Docker). No Windows, `validate()` retorna `indeterminate` com `unsupported_platform` **antes** de iniciar qualquer processo, e a capacidade `engines.verapdf` aparece indisponível com esse motivo. As demais operações continuam funcionando no Windows; trabalhos `pdfa` falham de forma fechada.
- Testes de execução real de processo (timeout, limite de relatório, descendentes) rodam apenas em POSIX; os testes do parser usam saída injetada em processo e rodam em todas as plataformas.

## Configuração (somente servidor)

- `VERAPDF_EXECUTABLE`: caminho do executável (sem parsing de shell; nunca vem da requisição). Ausente → motivo `validator_not_configured`.
- `VERAPDF_TIMEOUT_SECONDS` (padrão 120, 1–3600), registrado em `options_effective.pdfa_validation_timeout_seconds`.
- Docker: copia `/opt/verapdf` e `/opt/java/openjdk` da imagem oficial `verapdf/cli` 1.30.2 fixada por digest. Não há socket Docker montado nem serviço validador exposto. A compatibilidade dessa cópia com `python:3.11-slim` **ainda precisa ser verificada no build**.

## Validação final

- Para `output_format=pdfa`, após conversão/compressão o orquestrador valida o arquivo final como PDF/A-2b. O relatório (`output_validation`) registra `pdfa_conformance` (`passed`/`failed`/`indeterminate`), perfil, validador e versão, hash validado, códigos de motivo e estatísticas.
- Só `passed` conclui o trabalho. `failed` ou `indeterminate` persistem o relatório e então marcam o trabalho como `failed` (`pdfa_validation_failed` / `pdfa_validation_indeterminate`).
- `pdf_searchable` e `extract_text` não são validados nem declarados PDF/A (`not_validated`, aviso `pdfa_conformance_not_validated`). Trabalhos legados (sem opções) mantêm `not_validated`.
- `extract_text` com `pdfa` é recusado (422) na admissão e novamente na finalização; uma cópia nunca é rotulada como PDF/A.
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

Os padrões seguem a API (`pdfa`/`balanced`) quando oferecidos pelas capacidades; os seletores listam apenas valores reais. Se o validador estiver indisponível, a nota de capacidades exibe o motivo.

## Limites

- Um descendente que crie a própria sessão escapa do `killpg`.
- A derivada não preserva assinaturas digitais do original; `passed` não declara isso.
- Testes unitários usam veraPDF/OCRmyPDF falsos e só comprovam regras do wrapper e da integração. Medições pontuais (OCRmyPDF 16.13.0, 9 conversões sintéticas e um caso positivo veraPDF 1.30.2 born-digital/balanced) não constituem benchmark completo nem homologação de produção.
