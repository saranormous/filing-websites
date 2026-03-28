# Filing Websites Pipeline
# Usage:
#   make add PDF=prospectus.pdf SLUG=mycompany    # Full pipeline
#   make add PDF=https://example.com/p.pdf SLUG=x # From URL
#   make estimate PDF=prospectus.pdf              # Cost estimate
#   make render SLUG=mycompany                    # Re-render from data
#   make render-all                               # Re-render all sites
#   make index                                    # Rebuild index.html
#   make push                                     # Commit & push changes

PYTHON := python3

.PHONY: add estimate render render-all index push help

help:
	@echo "Filing Websites Pipeline"
	@echo ""
	@echo "  make add PDF=<file_or_url> SLUG=<name>  Process a new prospectus"
	@echo "  make estimate PDF=<file_or_url>          Show cost/time estimate"
	@echo "  make render SLUG=<name>                  Re-render one site"
	@echo "  make render-all                          Re-render all sites"
	@echo "  make index                               Rebuild index.html"
	@echo "  make push MSG=\"commit message\"            Commit and push"
	@echo ""
	@echo "Examples:"
	@echo "  make add PDF=prospectus.pdf SLUG=deepseek"
	@echo "  make add PDF=https://hkexnews.hk/doc.pdf SLUG=deepseek"

add:
ifndef PDF
	$(error PDF is required. Usage: make add PDF=prospectus.pdf SLUG=mycompany)
endif
ifndef SLUG
	$(error SLUG is required. Usage: make add PDF=prospectus.pdf SLUG=mycompany)
endif
	$(PYTHON) pipeline.py --yes $(PDF) $(SLUG)
	$(PYTHON) pipeline.py --rebuild-index
	@echo ""
	@echo "Done! Don't forget to:"
	@echo "  1. Add entry to filings.json"
	@echo "  2. make push MSG='Add $(SLUG) prospectus'"

estimate:
ifndef PDF
	$(error PDF is required. Usage: make estimate PDF=prospectus.pdf)
endif
	$(PYTHON) pipeline.py --estimate $(PDF)

render:
ifndef SLUG
	$(error SLUG is required. Usage: make render SLUG=mycompany)
endif
	$(PYTHON) pipeline.py --render $(SLUG)

render-all:
	@for dir in */data.json; do \
		slug=$$(dirname $$dir); \
		echo "Rendering $$slug..."; \
		$(PYTHON) pipeline.py --render $$slug; \
	done
	$(PYTHON) pipeline.py --rebuild-index

index:
	$(PYTHON) pipeline.py --rebuild-index

push:
	git add -A
	git commit -m "$(or $(MSG),Update filing sites)"
	git push origin main
