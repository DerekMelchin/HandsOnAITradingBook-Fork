# region imports
from AlgorithmImports import *

from scipy.optimize import minimize

import torch
from ast import literal_eval
from pathlib import Path
from functools import partial
from transformers import Trainer, TrainingArguments, set_seed 
from gluonts.dataset.pandas import PandasDataset
from gluonts.itertools import Filter
from chronos import ChronosConfig, ChronosPipeline
from chronos.scripts.training.train import ChronosDataset, has_enough_observations, load_model
from chronos.scripts.training import train
from logging import getLogger, INFO
# endregion

class HuggingFaceFineTunedDemo(QCAlgorithm):
    """
    This algorithm demonstrates how to fine-tune a HuggingFace model.
    It uses the "amazon/chronos-t5-tiny" model to forecast the 
    future equity curves of the 5 most liquid assets in the market,
    then it uses the SciPy package to find the portfolio weights
    that will maximize the future Sharpe ratio of the portfolio. 
    The model is retrained and the portfolio is rebalanced every 3 
    months.
    """

    def initialize(self):
        self.set_start_date(2019, 1, 1)
        self.set_end_date(2024, 4, 1)
        self.set_cash(100_000)

        # Disable the daily precise end time because of the time rules.
        self.settings.daily_precise_end_time = False
        self.settings.min_absolute_portfolio_target_percentage = 0

        # Define the universe.
        spy = Symbol.create("SPY", SecurityType.EQUITY, Market.USA)
        self.universe_settings.schedule.on(self.date_rules.month_start(spy))
        self.universe_settings.resolution = Resolution.DAILY
        self._universe = self.add_universe(
            self.universe.dollar_volume.top(
                self.get_parameter('universe_size', 5)
            )
        )

        # Define some trading parameters.
        self._lookback_period = timedelta(
            365 * self.get_parameter('lookback_years', 1)
        )
        self._prediction_length = 3*21  # Three months of trading days

        # Schedule rebalances.
        self._last_rebalance = datetime.min
        self.schedule.on(
            self.date_rules.month_start(spy, 1), 
            self.time_rules.midnight, 
            self._trade
        )
        
        # Add warm up so the algorithm trades on deployment.
        self.set_warm_up(timedelta(31))

        # Define the model and some of its settings.
        self._device_map = "cuda" if torch.cuda.is_available() else "cpu"
        self._optimizer = 'adamw_torch_fused' if torch.cuda.is_available() else 'adamw_torch'
        self._model_name = "amazon/chronos-t5-tiny"
        self._model_path = self.object_store.get_file_path(
            f"llm/fine-tune/{self._model_name.replace('/', '-')}/"
        )

    def on_warmup_finished(self):
        # Trade right after warm up is done.
        self._trade()

    def _sharpe_ratio(
            self, weights, returns, risk_free_rate, trading_days_per_year=252):
        # Define how to calculate the Sharpe ratio so we can use
        # it to optimize the portfolio weights.

        # Calculate the annualized returns and covariance matrix.
        mean_returns = returns.mean() * trading_days_per_year 
        cov_matrix = returns.cov() * trading_days_per_year

        # Calculate the Sharpe ratio.
        portfolio_return = np.sum(mean_returns * weights)
        portfolio_std = np.sqrt(np.dot(weights.T, np.dot(cov_matrix, weights)))
        sharpe_ratio = (portfolio_return - risk_free_rate) / portfolio_std
        
        # Return negative Sharpe ratio because we minimize this
        # function in optimization.
        return -sharpe_ratio

    def _optimize_portfolio(self, equity_curves):
        returns = equity_curves.pct_change().dropna()
        num_assets = returns.shape[1]
        initial_guess = num_assets * [1. / num_assets,]
        # Find portfolio weights that mazimize the forward Sharpe
        # ratio.
        result = minimize(
            self._sharpe_ratio, 
            initial_guess, 
            args=(
                returns,
                self.risk_free_interest_rate_model.get_interest_rate(self.time)
            ), 
            method='SLSQP', 
            bounds=tuple((0, 1) for _ in range(num_assets)), 
            constraints=(
                {'type': 'eq', 'fun': lambda weights: np.sum(weights) - 1}
            )
        )    
        return result.x

    def _trade(self):
        # Don't rebalance during warm-up.
        if self.is_warming_up:
            return
        # Only rebalance on a quarterly basis.
        if self.time - self._last_rebalance < timedelta(80):
            return  
        self._last_rebalance = self.time

        symbols = list(self._universe.selected)

        # Get historical equity curves.
        history = self.history(symbols, self._lookback_period)['close'].unstack(0)

        # Gather the training data.
        training_data_by_symbol = {}
        for symbol in symbols:
            df = history[[symbol]].dropna()
            if df.shape[0] < 10: # Skip this asset if there is very little data
                continue
            adjusted_df = df.reset_index()[['time', symbol]]
            adjusted_df = adjusted_df.rename(columns={str(symbol.id): 'target'})
            adjusted_df['time'] = pd.to_datetime(adjusted_df['time'])
            adjusted_df.set_index('time', inplace=True)
            adjusted_df = adjusted_df.resample('D').asfreq()
            training_data_by_symbol[symbol] = adjusted_df
        tradable_symbols = list(training_data_by_symbol.keys())
        
        # Fine-tune the model.
        output_dir_path = self._train_chronos(
            list(training_data_by_symbol.values()),
            context_length=int(252/2), # 6 months
            prediction_length=self._prediction_length,
            optim=self._optimizer,
            model_id=self._model_name,
            output_dir=self._model_path,
            learning_rate=1e-5,
            # Requires Ampere GPUs (e.g., A100)
            tf32=False,
            max_steps=3
        )

        # Load the fine-tuned model.
        pipeline = ChronosPipeline.from_pretrained(
            output_dir_path,
            device_map=self._device_map,
            torch_dtype=torch.bfloat16,
        )

        # Forecast the future equity curves.
        all_forecasts = pipeline.predict(
            [
                torch.tensor(history[symbol].dropna())
                for symbol in tradable_symbols
            ], 
            self._prediction_length
        )

        # Take the median forecast for each asset.
        forecasts_df = pd.DataFrame(
            {
                symbol: np.quantile(
                    all_forecasts[i].numpy(), 0.5, axis=0   # 0.5 = median
                )
                for i, symbol in enumerate(tradable_symbols)
            }
        )

        # Find the weights that maximize the forward Sharpe 
        # ratio of the portfolio.
        optimal_weights = self._optimize_portfolio(forecasts_df)

        # Rebalance the portfolio.
        self.set_holdings(
            [
                PortfolioTarget(symbol, optimal_weights[i])
                for i, symbol in enumerate(tradable_symbols)
            ], 
            True
        )

    def _train_chronos(
            self, training_data,
            probability: Optional[str] = None,
            context_length: int = 512,
            prediction_length: int = 64,
            min_past: int = 64,
            max_steps: int = 200_000,
            save_steps: int = 50_000,
            log_steps: int = 500,
            per_device_train_batch_size: int = 32,
            learning_rate: float = 1e-3,
            optim: str = "adamw_torch_fused",
            shuffle_buffer_length: int = 100,
            gradient_accumulation_steps: int = 2,
            model_id: str = "google/t5-efficient-tiny",
            model_type: str = "seq2seq",
            random_init: bool = False,
            tie_embeddings: bool = False,
            output_dir: str = "./output/",
            tf32: bool = True,
            torch_compile: bool = True,
            tokenizer_class: str = "MeanScaleUniformBins",
            tokenizer_kwargs: str = "{'low_limit': -15.0, 'high_limit': 15.0}",
            n_tokens: int = 4096,
            n_special_tokens: int = 2,
            pad_token_id: int = 0,
            eos_token_id: int = 1,
            use_eos_token: bool = True,
            lr_scheduler_type: str = "linear",
            warmup_ratio: float = 0.0,
            dataloader_num_workers: int = 1,
            max_missing_prop: float = 0.9,
            num_samples: int = 20,
            temperature: float = 1.0,
            top_k: int = 50,
            top_p: float = 1.0):

        # Set up logging for the train object.
        train.logger = getLogger()
        train.logger.setLevel(INFO)
        # Ensure output_dir is a Path object.
        output_dir = Path(output_dir)
        # Convert probability from string to a list, or set default if 
        # None.
        if isinstance(probability, str):
            probability = literal_eval(probability)
        elif probability is None:
            probability = [1.0 / len(training_data)] * len(training_data)
        # Convert tokenizer_kwargs from string to a dictionary.
        if isinstance(tokenizer_kwargs, str):
            tokenizer_kwargs = literal_eval(tokenizer_kwargs)
        # Enable reproducibility.
        set_seed(1, True)
        # Create datasets for training, filtered by criteria.
        train_datasets = [
            Filter(
                partial(
                    has_enough_observations,
                    min_length=min_past + prediction_length,
                    max_missing_prop=max_missing_prop,
                ),
                PandasDataset(data_frame, freq="D"),
            )
            for data_frame in training_data
        ]
        # Load the model with the specified configuration.
        model = load_model(
            model_id=model_id,
            model_type=model_type,
            vocab_size=n_tokens,
            random_init=random_init,
            tie_embeddings=tie_embeddings,
            pad_token_id=pad_token_id,
            eos_token_id=eos_token_id,
        )
        # Define the configuration for the Chronos 
        # tokenizer and other settings.
        chronos_config = ChronosConfig(
            tokenizer_class=tokenizer_class,
            tokenizer_kwargs=tokenizer_kwargs,
            n_tokens=n_tokens,
            n_special_tokens=n_special_tokens,
            pad_token_id=pad_token_id,
            eos_token_id=eos_token_id,
            use_eos_token=use_eos_token,
            model_type=model_type,
            context_length=context_length,
            prediction_length=prediction_length,
            num_samples=num_samples,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
        )

        # Add extra items to model config so that 
        # it's saved in the ckpt.
        model.config.chronos_config = chronos_config.__dict__
        # Create a shuffled training dataset with the 
        # specified parameters.
        shuffled_train_dataset = ChronosDataset(
            datasets=train_datasets,
            probabilities=probability,
            tokenizer=chronos_config.create_tokenizer(),
            context_length=context_length,
            prediction_length=prediction_length,
            min_past=min_past,
            mode="training",
        ).shuffle(shuffle_buffer_length=shuffle_buffer_length)

        # Define the training arguments.
        training_args = TrainingArguments(
            output_dir=str(output_dir),
            per_device_train_batch_size=per_device_train_batch_size,
            learning_rate=learning_rate,
            lr_scheduler_type=lr_scheduler_type,
            warmup_ratio=warmup_ratio,
            optim=optim,
            logging_dir=str(output_dir / "train-logs"),
            logging_strategy="steps",
            logging_steps=log_steps,
            save_strategy="steps",
            save_steps=save_steps,
            report_to=["tensorboard"],
            max_steps=max_steps,
            gradient_accumulation_steps=gradient_accumulation_steps,
            dataloader_num_workers=dataloader_num_workers,
            tf32=tf32,  # remove this if not using Ampere GPUs (e.g., A100)
            torch_compile=torch_compile,
            ddp_find_unused_parameters=False,
            remove_unused_columns=False,
        )

        # Create a Trainer instance for training the model.
        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=shuffled_train_dataset,
        )
        # Start the training process.
        trainer.train()
        # Save the trained model to the output directory.
        model.save_pretrained(output_dir)
        # Return the path to the output directory.
        return output_dir
