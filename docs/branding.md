# Marca da instalação

A distribuição inclui somente o nome textual Leadkit e uma base CSS própria. As fontes PDF
Source Sans Pro e Source Serif Pro conservam as licenças OFL em `app/assets/fonts/`.
Os logos textuais e os ícones geométricos da interface são código próprio sob a licença MIT raiz.

Para usar uma marca privada, monte uma pasta externa somente leitura e configure
`LEADKIT_BRAND_DIR=/caminho/absoluto/marca`. API e worker devem receber a mesma pasta e configuração.
Ela contém um `brand.json`, por exemplo:

```json
{"name":"Ateliê fictício", "logos":{"preta":"logo.svg", "branca":"logo-clara.svg"}}
```

As chaves de logo são opcionais. Recursos padrão ausentes usam o kit neutro livre. A pasta pode incluir `print.css`, fontes e imagens próprias.
Macros `brand.logo(variante, classes)`, `brand.running_header()`, `brand.wine()` e as demais
assinaturas continuam disponíveis; as classes `ac-*` são aliases de compatibilidade visual.
`brand:logo-preta.svg` e `brand:logo-branca.svg` são resolvidos dentro da raiz. Templates antigos
com caminhos do kit anterior são resolvidos pelo buscador como aliases de recursos da marca,
sem carregar ou redistribuir o kit anterior.

A configuração rejeita caminhos absolutos de recurso, rede, traversal e links simbólicos que
escapem da raiz. Não há escrita nem download de arquivos. Não monte conteúdo não confiável como
marca; a operadora administra essa pasta. Exportar um formulário não exporta o overlay.

O ensaio sintético em `tests_app/test_brand_overlay.py` verifica leitura sem escrita, escape de
texto e confinamento. Templates publicados continuam imutáveis; apenas sua marca externa muda.
