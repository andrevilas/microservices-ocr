# Homologação dos presets de compactação (OCR-EV-01)

Registro da homologação dos presets para `compression_preset_version` `2026-10-16.1`. Decisão:

- `none`, `balanced` e `preservation` estão **homologados**.
- `economic` foi **retirado**.

Só os presets homologados aparecem em `capabilities.compression_profiles`. O `economic` aparece em `withdrawn_compression_profiles` e um trabalho novo com ele recebe `422 compression_profile_unavailable`. A mesma recusa vale na revalidação do worker, então um trabalho admitido antes da retirada também não roda. Relatórios e opções gravados enquanto ele era oferecido continuam legíveis (`tests/test_pdf_signatures.py`, fixtures em `tests/fixtures/legacy_reports/`).

## Como reproduzir

- **Corpus:** `benchmarks/presets/corpus.py` gera um corpus sintético e determinístico. Nenhum documento real é lido.
- **Execução:** `benchmarks/presets/run.py` roda a mesma cadeia do produto:
  1. OCR para PDF pesquisável, com os argumentos fixos;
  2. a conversão final de cada preset, com os argumentos tirados de `preset_arguments`;
  3. o veraPDF sobre os bytes finais.
- **Ambiente da execução de 2026-10-22:**
  - imagem de produção `localhost/ocr-recognizer:sha-319a704`;
  - OCRmyPDF 16.13.0, Ghostscript 10.05.1, Tesseract 5.5.0 e veraPDF 1.30.2;
  - contêiner com `--network none --cpus 2 --memory 2g --memory-swap 2g`.
- **Resultado bruto:** `benchmarks/presets/results-2026-10-22.json` (SHA-256 `626d721322be57827f4122e9186cf281602982b0be1800674ddc50f308d91e0a`).

```sh
docker run --rm --network none --cpus 2 --memory 2g --memory-swap 2g \
  -v "$PWD:/src:ro" -v "$OUT:/out" -e PYTHONPATH=/src \
  --entrypoint python localhost/ocr-recognizer:sha-<tag> /src/benchmarks/presets/run.py /out
```

## Corpus

| Caso | Conteúdo |
| --- | --- |
| `digital` | texto nativo, 2 páginas |
| `scan-300-gray` | digitalização em cinza, 300 dpi, JPEG |
| `scan-150-lowres` | digitalização de baixa resolução, 150 dpi |
| `scan-small-print` | 300 dpi com letra pequena (cerca de 8 pt) |
| `scan-color-stamp` | colorida, com logotipo, degradê, traço de caneta e carimbo vermelho |
| `scan-table` | tabela com linhas finas |
| `scan-a3` | página A3, 300 dpi |
| `scan-bilevel` | bitonal (estilo fax), PNG |
| `mixed` | página digital seguida de digitalização colorida |
| `already-pdfa` | saída PDF/A do `balanced` reprocessada |

Originais assinados não entram aqui. A assinatura nunca sobrevive ao OCR em nenhum preset; o tratamento deles está em `docs/pdfa.md`.

## Critérios

**v1, fixada antes de qualquer execução.** Comparava cada preset com a **entrada**:
- texto idêntico ao do OCR;
- CER da releitura até 0,01 acima do da entrada;
- PSNR de pelo menos 30 dB.

O `preservation`, que é sem perdas, reprovou exatamente nos mesmos pontos que os outros presets: PSNR de 26,86 dB na tabela, palavras unidas na extração da tabela e CER de 0,044 para 0,070 no misto. Isso mostrou que a v1 media a conversão PDF/A do Ghostscript, comum a todos, e não o preset. Os dados da v1 continuam no resultado, nos campos `pipeline_*`.

**v2, registrada como revisão e usada na decisão.** Usa os mesmos limiares, mas compara cada preset com a saída do `preservation` da mesma cadeia. Acrescenta uma prova direta de que o `preservation` não tem perdas. Um preset só é homologado se todos os casos atenderem a todos os critérios.

| Critério | Regra |
| --- | --- |
| PDF/A | veraPDF PDF/A-2b `passed` sobre os bytes finais (hash validado igual ao do arquivo) |
| Páginas | mesmo número de páginas e tamanhos exatamente iguais aos da entrada |
| Camada de texto | texto extraído igual ao do `preservation` |
| Legibilidade | CER da releitura (Tesseract a 300 dpi) até 0,01 acima do `preservation`, em cada página digitalizada |
| Fidelidade visual | PSNR a 150 dpi contra o `preservation` de pelo menos 30 dB |
| Sem perdas | só para `preservation`: cada imagem decodifica exatamente nos pixels da entrada |
| Recursos | pico de RSS da conversão até 1536 MiB |
| Propósito do `economic` | menor que o `balanced` em todo caso com imagem (até +1% sem imagem) e pelo menos 15% menor, em mediana, nos digitalizados |

## Resultados

Todos os 40 arquivos (10 casos × 4 presets) passaram no veraPDF com o hash conferido e preservaram páginas e tamanhos. Pico de RSS máximo de 128 MiB e conversão de até 2,4 s.

Tamanho final em bytes:

| Caso | none | balanced | preservation | economic | redução do economic sobre o balanced |
| --- | ---: | ---: | ---: | ---: | ---: |
| digital | 53.726 | 53.704 | 53.835 | 53.704 | 0% |
| scan-300-gray | 740.732 | 670.339 | 740.732 | 199.914 | 70% |
| scan-150-lowres | 205.132 | 189.371 | 205.132 | 65.279 | 66% |
| scan-small-print | 832.680 | 770.537 | 832.680 | 261.208 | 66% |
| scan-color-stamp | 862.998 | 787.412 | 862.998 | 251.531 | 68% |
| scan-table | 843.142 | 741.461 | 843.143 | 236.519 | 68% |
| scan-a3 | 1.470.369 | 1.331.542 | 1.470.369 | 391.748 | 71% |
| scan-bilevel | 62.469 | 62.455 | 62.470 | 21.154 | 66% |
| mixed | 863.691 | 783.952 | 863.803 | 264.100 | 66% |
| already-pdfa | 53.755 | 53.735 | 53.873 | 53.735 | 0% |

**`none`, `balanced` e `preservation`:**
- todos os critérios atendidos;
- nos três, as imagens decodificam **pixel a pixel iguais** à entrada em todos os casos;
- o ganho do `balanced` (de 0% a 10%) vem do otimizador sem perdas, não da recodificação das imagens.

**`economic`:**
- reduz de 66% a 71% sobre o `balanced` nos digitalizados;
- PSNR de 46 a 49 dB contra o `preservation`;
- legibilidade igual em quase todos os casos;
- **reprovou** no `scan-color-stamp`: o CER da releitura subiu de 0,029 para 0,061, mais que o dobro. O JPEG com qualidade 75 degrada o texto sobre fundo colorido e perto do carimbo, que são justamente os casos em que perder legibilidade importa. Por isso foi retirado em vez de anunciado com ressalva.

## Achados da cadeia (independentes do preset)

- **Renderização:** a conversão PDF/A do Ghostscript altera a renderização das páginas (de 26,9 a 35 dB contra a entrada, exceto no A3, que sai idêntico), embora as imagens continuem iguais pixel a pixel. Isso afeta todos os presets e não é uma perda de imagem.
- **Extração de texto da tabela:** depois da conversão, a extração do `scan-table` une palavras ("Data16/10/2"). O texto canônico do produto vem do OCR, antes da conversão, então isso não chega ao `pages.jsonl`. Afeta quem extrai texto do PDF final.
- **Referência da tabela:** o CER da releitura da tabela fica em cerca de 0,80 até na entrada, porque a ordem de leitura de uma tabela não corresponde à referência linear. Ali a métrica só vale de forma relativa entre presets.

## Limites

- **Corpus:** sintético e pequeno, com uma página por caso, exceto `digital` e `mixed`. Não mede a precisão do OCR em acervo real nem documentos de 300 páginas.
- **Recursos:** medidos com 2 CPUs e 2 GiB, em execução sequencial. Não dimensionam concorrência no TrueNAS.
- **Reabertura:** qualquer mudança nos argumentos de um preset exige nova `COMPRESSION_PRESET_VERSION` e nova execução deste benchmark. Isso vale também para uma variante menos agressiva do `economic`, como JPEG 85 ou só o otimizador nível 2, que seria um preset novo a homologar.
