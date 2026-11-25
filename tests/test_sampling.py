"""The optional sampling keys in [local] (seed, top_k, ...) reach the model request, so every model can be sampled the same
way and a seeded run repeats."""
import unittest

from mini.agent.core import Agent
from tests.helpers import temp_config


class SamplingTest(unittest.TestCase):
    def test_only_temperature_by_default(self):
        a = Agent(temp_config(), label="s")
        self.assertEqual(a.model_options,
                         {"temperature": a.cfg["local"]["temperature"]})

    def test_seed_and_sampling_keys_pass_through(self):
        cfg = temp_config()
        cfg["local"].update(seed=3, top_k=40, top_p=0.95, min_p=0.05, unknown=1)
        a = Agent(cfg, label="s")
        for k, v in dict(seed=3, top_k=40, top_p=0.95, min_p=0.05).items():
            self.assertEqual(a.model_options[k], v)
        self.assertNotIn("unknown", a.model_options)


if __name__ == "__main__":
    unittest.main()
