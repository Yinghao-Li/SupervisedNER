import gc
import logging
from typing import Optional

import torch
import wandb
from seqlbtoolkit.base_model.train import BaseTrainer
from seqlbtoolkit.base_model.eval import get_ner_metrics
from tqdm.auto import tqdm

from .args import Config
from .dataset import Dataset
from .collator import collator
from .model import BiRnnCrf
from ..base.status import Status

logger = logging.getLogger(__name__)


class Trainer(BaseTrainer):
    """
    Bert trainer used for training BERT for token classification (sequence labeling)
    """

    def __init__(self,
                 config: Config,
                 collate_fn=collator,
                 model=None,
                 training_dataset: Optional[Dataset] = None,
                 valid_dataset: Optional[Dataset] = None,
                 test_dataset: Optional[Dataset] = None):

        super().__init__(
            config=config,
            training_dataset=training_dataset,
            valid_dataset=valid_dataset,
            test_dataset=test_dataset,
            collate_fn=collate_fn
        )

        self._model = model
        self._optimizer = None
        self._loss_fn = None
        self._status = Status()
        self.initialize()

    @property
    def training_dataset(self):
        return self._training_dataset

    @property
    def valid_dataset(self):
        return self._valid_dataset

    @property
    def test_dataset(self):
        return self._test_dataset

    def initialize_model(self):
        self._model = BiRnnCrf(config=self.config)
        return self

    def initialize_optimizer(self, optimizer=None):
        """
        Initialize training optimizer
        """
        if optimizer is not None:
            self._optimizer = optimizer
        else:
            self._optimizer = torch.optim.AdamW(
                self._model.parameters(),
                lr=self._config.learning_rate,
                weight_decay=self.config.weight_decay
            )
        return self

    def run(self):
        self._model.to(self._device)

        self._status.init(self.config.model_buffer_size, False)
        # ----- start training process -----
        logger.info("Start training...")
        for epoch_i in range(self._config.num_train_epochs):
            logger.info("------")
            logger.info(f"Epoch {epoch_i + 1} of {self._config.num_train_epochs}")

            training_dataloader = self.get_dataloader(self._training_dataset, shuffle=True)

            train_loss = self.training_step(training_dataloader)
            logger.info("Training loss: %.4f" % train_loss)

            self.eval_and_save()

        test_results = self.test()
        for k, v in test_results['micro avg'].items():
            wandb.run.summary[f"test/{k}"] = v
        self.save_results_as_wandb_table(test_results)

        logger.info("Test results:")
        self.log_results(test_results)

        wandb.finish()

        gc.collect()
        torch.cuda.empty_cache()

        return None

    def training_step(self, data_loader):
        """
        Implement each training loop
        """
        train_loss = 0

        self._model.train()
        self._optimizer.zero_grad()

        for idx, inputs in enumerate(tqdm(data_loader)):
            # get data
            inputs.to(self._device)

            # training step
            loss = self._model.loss(inputs)
            loss.backward()
            # track loss
            train_loss += loss.detach().cpu()
            self._optimizer.step()
            self._optimizer.zero_grad()

        return train_loss

    def eval_and_save(self):
        """
        Evaluate the model and save it if its performance exceeds the previous highest
        """
        valid_results = self.evaluate(self.valid_dataset)

        logger.info("Validation results:")
        self.log_results(valid_results)

        step_idx = self._status.eval_step + 1

        result_dict = {f"valid/{k}": v for k, v in valid_results.items()}
        wandb.log(data=result_dict, step=step_idx)

        # ----- check model performance and update buffer -----
        if self._status.model_buffer.check_and_update(valid_results.f1, self.model):
            logger.info("Model buffer is updated!")

        self._status.eval_step += 1
        return None

    def evaluate(self, dataset: Dataset, detailed_result: Optional[bool] = False):
        data_loader = self.get_dataloader(dataset)
        self._model.to(self._device)
        self._model.eval()

        pred_lbs = list()
        with torch.no_grad():
            for inputs in data_loader:
                inputs.to(self._device)

                _, pred_ids = self._model(inputs)

                pred_lb_batch = [[self.config.bio_label_types[lb_index] for lb_index in label_indices]
                                 for label_indices in pred_ids]
                pred_lbs += pred_lb_batch

        true_lbs = [[self.config.bio_label_types[lb_index] for lb_index in label_indices]
                    for label_indices in dataset.lbs]
        metric = get_ner_metrics(true_lbs, pred_lbs, detailed=detailed_result)
        return metric

    def test(self):

        if self._status.model_buffer.size == 1:
            self._model.load_state_dict(self._status.model_buffer.model_state_dicts[0])
            metrics = self.evaluate(self._test_dataset, detailed_result=True)
            return metrics

        raise NotImplementedError("Function for multi-checkpoint caching & evaluation is not implemented!")

    @staticmethod
    def log_results(metrics):
        if isinstance(metrics, dict):
            for key, val in metrics.items():
                logger.info(f"[{key}]")
                for k, v in val.items():
                    logger.info(f"  {k}: {v:.4f}.")
        else:
            for k, v in metrics.items():
                logger.info(f"  {k}: {v:.4f}.")

    @staticmethod
    def save_results_as_wandb_table(metrics: dict, table_name: Optional[str] = 'test_results'):
        """
        Save dictionary results in a wandb table
        """
        columns = ['Entity Type', 'Precision', 'Recall', 'F1']
        tb = wandb.Table(columns)

        for ent, metrics in metrics.items():
            row = [ent] + [value for value in metrics.values()]
            tb.add_data(*row)
        wandb.run.log({table_name: tb})

        return None