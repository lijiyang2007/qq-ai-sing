import unittest
import numpy as np
from hq_cover.mixing import mix_instrumental_and_converted

class TestBoundaryFreeMixing(unittest.TestCase):
 def test_source_mix_is_not_an_input_to_full_track_mix(self):
  inst=np.full((1000,2),.6,dtype=np.float32);converted=np.full(1000,.3,dtype=np.float32)
  y=mix_instrumental_and_converted(inst,converted,2.)
  np.testing.assert_allclose(y,1.2)
 def test_intro_middle_and_outro_use_identical_formula(self):
  inst=np.arange(20,dtype=np.float32).reshape(10,2)/100;converted=np.linspace(-.2,.2,10,dtype=np.float32)
  y=mix_instrumental_and_converted(inst,converted,1.5)
  expected=inst+converted[:,None]*1.5
  np.testing.assert_allclose(y,expected);np.testing.assert_allclose(y[[0,5,9]],expected[[0,5,9]])
 def test_no_gate_module_is_needed(self):
  import hq_cover.mixing as m
  self.assertFalse(hasattr(m,'replacement_weight'));self.assertFalse(hasattr(m,'blend_preserving_instrumental'))
if __name__=='__main__':unittest.main()
