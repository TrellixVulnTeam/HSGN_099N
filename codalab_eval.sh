cl run --request-docker-image naster94/exo --request-network --request-gpus 1 --request-cpus 4 --request-memory 32g repo:0x081594 HSGN/data/external/input.json:hotpotqa-data//dev_distractor_input_v1.0 HSGN/models/graph_model:0xda36f2 doc_retrieval:0x2a271b 'cp -r repo/HSGN . ; cp -r doc_retrieval/ HSGN/models/ ;cd HSGN;  python run_prediction.py; cp pred.json ../' -n run_pred



cl run --request-docker-image naster94/exo:1.1 --request-gpus 1 --request-cpus 4 --request-memory 32g HSGN/data/external/input.json:hotpotqa-data//dev_distractor_input_v1.0  './run.sh; cp pred.json ../'