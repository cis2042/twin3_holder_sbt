"""
Report Generator.

Renders analysis results into a scrollytelling HTML report
using Jinja2 templates with GSAP ScrollTrigger + Chart.js.
"""

import logging
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

import config

logger = logging.getLogger(__name__)


class ReportGenerator:
    """Renders analysis data into scrollytelling HTML reports."""

    def __init__(self):
        self.template_dir = config.TEMPLATE_DIR
        self.output_dir = config.REPORT_OUTPUT_DIR
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=False,
        )

    def generate(self, report_data: dict) -> Path:
        """Generate the scrollytelling HTML report."""
        logger.info("Generating HTML report...")

        template = self.env.get_template("report.html")
        html = template.render(**report_data)

        report_date = report_data.get("report_date", datetime.now().strftime("%Y-%m-%d"))
        filename = f"bsc_token_report_{report_date}.html"
        output_path = self.output_dir / filename

        output_path.write_text(html, encoding="utf-8")
        logger.info(f"Report saved to: {output_path}")

        return output_path
