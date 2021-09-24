import yaml
import csv
import torch
import functorch
import re
import sys
import os

class CapturedOutput(object):
    """
    Class used to grab standard output.
    We need this instead of contextlib.redirect_stdout() if the printed text
    that we want to capture comes from C++.
    The result is stored in capturedtext.
    Pulled partially from https://www.py4u.net/discuss/66399.
    """
    escape_char = "\b"

    def __init__(self):
        self.origstream = sys.stdout
        self.origstreamfd = self.origstream.fileno()
        self.capturedtext = ""
        # Create a pipe so the stream can be captured:
        self.pipe_out, self.pipe_in = os.pipe()

    def __enter__(self):
        self.capturedtext = ""
        # Save a copy of the stream:
        self.streamfd = os.dup(self.origstreamfd)
        # Replace the original stream with our write pipe:
        os.dup2(self.pipe_in, self.origstreamfd)
        return self

    def __exit__(self, type, value, traceback):
        # Print the escape character to make the readOutput method stop:
        self.origstream.write(self.escape_char)
        # Flush the stream to make sure all our data goes in before
        # the escape character:
        self.origstream.flush()
        self.readOutput()
        # Close the pipe:
        os.close(self.pipe_in)
        os.close(self.pipe_out)
        # Restore the original stream:
        os.dup2(self.streamfd, self.origstreamfd)
        # Close the duplicate stream:
        os.close(self.streamfd)

    def readOutput(self):
        """
        Read the stream data (one byte at a time)
        and save the text in `capturedtext`.
        """
        while True:
            char = os.read(self.pipe_out, 1)
            if not char:
                break
            char = char.decode("utf-8")
            if self.escape_char in char:
                break
            self.capturedtext += char

def get_ops_for_key(key):
    all_out = CapturedOutput()
    with all_out:
        if key is None:
            torch._C._dispatch_print_registrations_for_dispatch_key()
        else:
            torch._C._dispatch_print_registrations_for_dispatch_key(key)

    ops = all_out.capturedtext.split('\n')
    cleaned_ops = []
    for i in ops:
        if 'aten::' not in i:
            continue
        cleaned_ops.append(i[6:].strip())
    return set(cleaned_ops)

batched_registrations = get_ops_for_key('FuncTorchBatched')
all_ops = get_ops_for_key(None)

# Find all occurrences of things inside of STOP_DECOMPOSE(...) using regex
# Look in ../functorch/csrc/BatchRulesStopDecomposition.cpp
# Example:
# STOP_DECOMPOSE(sin); => sin
with open('../functorch/csrc/BatchRulesStopDecomposition.cpp') as f:
    content = f.read()
    stop_decomposition_regex = re.compile(r'STOP_DECOMPOSE\((.*)\);')
    stop_decomposition_matches = stop_decomposition_regex.findall(content)
    stop_decomposition_matches = [m.strip() for m in stop_decomposition_matches]
    stop_decomposition_ops = set(stop_decomposition_matches)

composite_ops = get_ops_for_key('CompositeImplicitAutograd')
decomposed_ops = composite_ops - stop_decomposition_ops


vmap_ops = (batched_registrations - stop_decomposition_ops) | (composite_ops - stop_decomposition_ops)
noncomposite_ops = all_ops - composite_ops

ops = yaml.load(open('/home/chilli/fb/pytorch/aten/src/ATen/native/native_functions.yaml', 'r').read())

annotated_ops = {a.strip(): b.strip() for a,b in list(csv.reader(open('annotated_ops.txt')))}
from collections import defaultdict

uniq_ops = []
uniq_names = set()
overload_types = defaultdict(list)
cnt = 0
for op in ops:
    func_str = op['func']
    name = func_str[:func_str.index('(')]
    if '.' in name:
        uniq_name = name[:name.index('.')]
        overload_types[name[name.index('.') + 1:]].append(name)
    else:
        uniq_name = name
    op['name'] = uniq_name
    full_name = func_str[:func_str.index('(')]
    op['full_name'] = full_name
    ret_type = func_str[func_str.index('->') + 3:]
    op['ret_type'] = ret_type
    cnt += 1
    if uniq_name in uniq_names:
        continue
    uniq_names.add(uniq_name)
    uniq_ops.append(op)

def annotate_ops(ops, is_unique):
    categorization = defaultdict(int)
    for i in ops:
        old_tcnt = sum(categorization.values())
        if i['name'][-1] == '_':
            categorization['inplace'] += 1
            i['meta'] = 'inplace'
            continue
        if not is_unique and 'a!' in i['func'].lower():
            categorization['out'] += 1
            i['meta'] = 'out'
            continue
        if 'conv' in i['name']:
            categorization['conv'] += 1
            i['meta'] = 'conv'
            continue
        if 'pool' in i['name']:
            categorization['pool'] += 1
            i['meta'] = 'pool'
            continue
        if 'backward' in i['name']:
            categorization['backward'] += 1
            i['meta'] = 'backward'
            continue
        if i['name'][0] == '_' and i['name'][1] != '_':
            categorization['private'] += 1
            i['meta'] = 'private'
            continue
        if 'batch_norm' in i['name']:
            categorization['batch_norm'] += 1
            i['meta'] = 'batch_norm'
            continue
        if 'Tensor' not in i['func'] or'Tensor' not in i['ret_type']:
            categorization['non_tensor'] += 1
            i['meta'] = 'non_tensor'
            continue
        if 'cudnn' in i['name'] or 'mkldnn' in i['name'] or 'miopen' in i['name'] or 'native' in i['name'] or 'thnn' in i['name'] or 'slow' in i['name']:
            categorization['backend'] += 1
            i['meta'] = 'backend'
            continue
        if i['name'] in annotated_ops:
            categorization['core'] += 1
            i['meta'] = 'core ' + annotated_ops[i['name']]
        else:
            categorization['core'] += 1
            i['meta'] = 'core unknown'
    return categorization

categorization = annotate_ops(uniq_ops, True)
categorization = annotate_ops(ops, False)

for op in ops:
    info = [op['full_name'], op['meta'], not (op['full_name'] in noncomposite_ops), op['full_name'] in vmap_ops]
    print(','.join([str(i) for i in info]))