# kevin-agent-kit

여러 코딩 에이전트의 구현, 리뷰, 통합을 로컬에서 조율하는 하네스입니다.

> one human holds the decision gate; an agent fleet does the rest

[English](../../README.md) · [저장소](https://github.com/JDeun/kevin-agent-kit.git)

사람이 작업 방향을 정하고, 검토된 설계안을 선택하며, 배포와 정리를 승인합니다.
에이전트는 작업을 조율하고 변경 사항을 검토하며, 격리된 워크트리에서 문제를 수정하고
검증 근거를 수집합니다.

## 드라이런으로 시작하기

Git과 Python 3.11 이상이 설치된 환경에서 소스 체크아웃을 열고 실행합니다.

```sh
python3 -m core.loop demo
```

데모는 임시 Git 저장소와 로컬 리뷰·작업 명령을 사용합니다.
모델 인증 정보나 호스팅 계정 없이 실행할 수 있습니다.

명령을 설치하려면 소스 체크아웃에서 빌드와 설치를 실행합니다.

```sh
./scripts/build.sh
./scripts/install.sh
kevin-agent-kit demo
```

`claude --plugin-dir ./dist/kevin-agent-kit/plugin`으로 Claude Code 플러그인을 불러옵니다.
플러그인을 쓰기 전에 설치한 명령의 디렉터리를 `PATH`에 추가합니다.
빌드 전에 `./scripts/rename.sh NEW`를 실행하면 `project.toml`의 `[project].name`이 바뀝니다.
빌드는 이 값을 CLI와 생성된 문서에 반영합니다. Python 패키지와 Claude Code 명령의
네임스페이스에는 하이픈을 밑줄로 바꾼 이름을 씁니다. 빌드 디렉터리를 `PYTHONPATH`에
추가하면 `python3 -m kevin_agent_kit`로 패키지를 실행할 수 있습니다.

## 프로젝트에 적용하기

프로젝트의 [TOML 설정](../loop.md)에 저장소, 대상 브랜치, 리뷰·작업·검증 명령을 지정합니다.
루프를 시작한 뒤 검토할 로컬 브랜치와 정확한 커밋을 등록하고 상태를 확인합니다.

```sh
kevin-agent-kit --config ./project.toml start
kevin-agent-kit --config ./project.toml enqueue feature/example "$(git rev-parse feature/example)"
kevin-agent-kit --config ./project.toml status
```

작업을 마치면 `kevin-agent-kit --config ./project.toml stop`으로 루프를 종료합니다.

소스 체크아웃에서는 설치 명령 대신 `python3 -m core.loop`를 쓸 수 있습니다.
실행 흐름과 사람이 결정하는 지점은 [아키텍처](../architecture.md)에,
GitHub와 GitLab 연동은 [호스팅 어댑터](../hosting.md)에 설명되어 있습니다.

- [작업자 규칙](../../agents/worker-rules.md)은 브랜치 범위와 리소스 소유권을 다룹니다.
- [페르소나 리뷰](../../reviews/persona/README.md)는 설계 대안과 의사결정 자료를 만듭니다.
- [스크린샷 검증](../../verify/README.md)으로 실행 중인 앱을 캡처해 PR이나 MR에 근거를 남길 수 있습니다.
- [가드](../guards.md)와 [장부](../ledgers.md)에서 검사 항목과 기록 방법을 확인할 수 있습니다.
- [비용과 컨텍스트](../cost.md)는 모델 선택과 세션 사용량 관리 방법을 설명합니다.

## 참고한 공개 프로젝트와 발표

| 출처 | 참고한 아이디어 |
| --- | --- |
| [Lauren Tan, All Code Is Slop (Except Mine), AI Engineer](https://ai.engineer/talks/O_zcc6Xl12g-all-code-is-slop-except-mine) | 반복되는 리뷰 판단을 자동 검사로 옮기고, 실행 중인 앱에서 검증 근거 수집하기 |
| [cursor/plugins의 pstack](https://github.com/cursor/plugins/blob/main/pstack/README.md) | 사람이 선택하기 전에 독립적인 설계 대안을 만들고 문제점을 검토하기 |
| [ECC](https://github.com/affaan-m/ECC) | 재사용할 수 있는 에이전트 규칙과 도구를 작업 흐름과 함께 배포하기 |
| [AgentShield](https://github.com/affaan-m/agentshield) | 결정적 검사와 독립적인 리뷰 결과를 모아 구체적인 발견 사항을 정리하기 |

위 출처의 아이디어를 참고해 이 저장소의 코드와 문서를 처음부터 작성했습니다.
