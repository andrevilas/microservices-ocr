# Validação PDF/A (OCR-EV-01B — parcial)

`app/services/pdfa_validator.py` encapsula o veraPDF como executável externo com comando fixo no servidor; requisições escolhem apenas o arquivo.

## Comportamento

- Valida uma **cópia privada congelada** em diretório temporário; o `sha256` retornado cobre exatamente os bytes validados. Se o arquivo de origem divergir desses bytes ao final, o resultado é `indeterminate` (`input_changed_during_validation`). O original nunca é alterado.
- Resultado: `passed`, `failed` ou `indeterminate`, com perfil, versão do validador, hash, códigos de motivo estáveis e estatísticas (`passedRules`, `failedRules`, `passedChecks`, `failedChecks`).
- Fail closed: relatório ausente, incompleto ou contraditório vira `indeterminate`. Exige exatamente um `batchSummary` com `totalJobs=1`, `failedToParse`, `encrypted`, `outOfMemory` e `veraExceptions` iguais a zero, `validationReports failedJobs=0` e `compliant`/`nonCompliant` coerentes com `isCompliant`. Os quatro contadores de `details` são obrigatórios e coerentes com o veredito.
- Contadores aceitam apenas dígitos ASCII (até 12); valores Unicode, sinais ou longos são rejeitados sem gerar `ValueError`.
- O processo roda em grupo próprio (`start_new_session` no Linux, `CREATE_NEW_PROCESS_GROUP` no Windows) com prazo único para saída e EOF. No timeout ou relatório grande demais, o grupo/árvore é encerrado (`killpg` / `taskkill /T /F`), com limpeza limitada e sem `close` bloqueante.

## Limites

- No Windows, se o processo líder já tiver saído e um descendente mantiver o pipe aberto, `taskkill /T` não alcança esse descendente (seria necessário usar Job Object). O alvo de produção é o Linux.
- Um descendente que crie a própria sessão escapa do `killpg`.
- A derivada não preserva assinaturas digitais do original; `passed` não declara isso.
- Os testes usam um veraPDF falso e só comprovam as regras do wrapper, não a conformidade real. Nenhum benchmark foi executado.

## Pendente (integração E1)

Ainda não implementado: validação final no orquestrador, configuração server-only do executável e do timeout, detecção de capacidade na API/MCP/UI, imagem Docker com veraPDF 1.30.2, presets `economic`/`balanced`/`preservation`, recusa de PDF/A em `extract_text` e harmonização dos padrões da UI.
