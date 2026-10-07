"""Retrieval interfaces. Heavy model dependencies are loaded only in RAG mode.

macOS 에서 faiss-cpu 와 PyTorch 가 각자 자기 libomp 를 프로세스에 올린다. 두 OpenMP
런타임이 함께 있는 상태에서 파이썬 스레드 안에서 병렬 구간에 들어가면 프로세스가
``__kmp_fork_barrier`` 안에서 SIGSEGV 로 죽는다. 검색은 질문 단위 스레드 풀에서 돌고
그 안에서 질의 임베딩(torch)과 FAISS 조회가 연달아 일어나므로, live 모드가 시작 직후
매번 이 경합에 걸렸다 (크래시 리포트의 faulting thread 가 ``__kmp_launch_worker``).

라이브러리 자체의 내부 스레딩을 끄면 경합이 사라진다. 병렬성은 이미 파이썬 스레드
풀이 제공하고, 여기서 인코딩하는 것은 짧은 질의문뿐이라 처리량 손해는 거의 없다.
이 설정은 faiss·torch 가 로드되기 전에 정해져야 해서, 두 라이브러리를 지연 임포트하는
이 패키지의 import 시점에 둔다. 호출자가 값을 직접 지정했으면 그대로 존중한다.
"""

import os

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
