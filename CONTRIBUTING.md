# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary
information to effectively respond to your bug report or contribution.


## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment


## Contributing via Pull Requests

Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local tests pass (see below).
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional documentation on [forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).


## Before you open a PR

Run these from the repository root. `install_cdk.sh` creates `.venv` with `uv venv`; if you haven't run it, run `uv venv` first. The README's Tests section has more.

```bash
# Test requirements, into .venv.
uv pip install --python .venv -r backend/requirements-dev.txt

# Integration tests run against local Oracle, PostgreSQL, MySQL, MariaDB, DynamoDB Local and moto.
# Their admin passwords are generated into the git-ignored .env.test at the repository root,
# where docker-compose.test.yml and the tests both read it; none is in the repository.
for k in ORACLE_PASSWORD POSTGRES_PASSWORD MYSQL_ROOT_PASSWORD MARIADB_ROOT_PASSWORD; do
  echo "$k=Aa1$(openssl rand -hex 12)"; done > .env.test
docker compose -f docker-compose.test.yml up -d --wait

# Unit, then integration tests.
(cd backend/src && ../../.venv/bin/python -m pytest tests/unit -q &&
  ../../.venv/bin/python -m pytest tests/integration -q -m integration)

# Frontend: lint, then the build frontend/app.py runs. The build writes src/aws-exports.ts
# from a deployed backend's outputs file, so it needs one.
(cd frontend/src/adam_ui && npm ci && npm run lint &&
  OUTPUTS_FILE=../../backend_outputs_<stage>.json ./build_website_deployment.sh)
```


## Finding contributions to work on

Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.


## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.


## Security issue notifications

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue.


## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
