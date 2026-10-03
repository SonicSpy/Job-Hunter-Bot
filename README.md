# Job Hunter Bot

A free bot that searches for jobs every hour, all day and night, for Bhargav.

- **Where it looks:** official company careers pages (Greenhouse, Lever, Ashby, Workable, SmartRecruiters) for about 100 product companies, AI labs and gaming studios, plus 4 remote-job boards with public APIs (Remotive, RemoteOK, Himalayas, Jobicy).
- **What it keeps:** remote roles open to India, full-time, fresher to mid level, posted in the last 3 weeks, not below ₹8 LPA when a salary is shown. It skips IT-services companies, staffing agencies and scam signs.
- **Results:** `JOBS.md` (easy to read), `jobs.json` (read by the daily Claude routine that applies), `status.md` (which sources worked).
- **Cost:** ₹0. It runs on GitHub's free plan. It uses no AI and no Claude usage.

To change what it looks for, open `config.json` on GitHub, click the pencil icon, edit and press **Commit changes**.
