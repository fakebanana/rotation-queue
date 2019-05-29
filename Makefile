# -*- Makefile -*-

TAG := flex-repo.akam.ai/rotation-queue
BUILD := build.log
PUSH := push.log
DEPLOY := deploy.log
NAMESPACE := kube-system
APP := rotation-queue
NOW := $(shell date +'%s')


all: $(PUSH)

.PHONY: all build push clean

build: $(BUILD)
$(BUILD): Dockerfile requirements.txt rotation_queue.py
	docker build . -t $(TAG) 2>&1 | tee $(BUILD)

push: $(push)
$(PUSH): $(BUILD)
	docker push $(TAG) | tee $(PUSH)
	sleep 1
	kubectl -n $(NAMESPACE) patch ds $(APP) -p "{\"spec\":{\"template\":{\"metadata\":{\"labels\":{\"date\":\"$(NOW)\"}}}}}" | tee -a $(PUSH)

clean:
	docker rmi $(TAG)
