import torch
import copy
from torch.testing._internal.common_methods_invocations import op_db
from enum import Enum
import functorch._src.top_operators_github_usage as top_ops
import pprint

# Importing these files make modifications to the op_db that we need
import test_ops  # noqa: F401
import test_vmap  # noqa: F401

all_overridable = list(torch.overrides.get_testing_overrides().keys())

public_docs = [
    (torch.nn.functional, 'torch.nn.functional', 'docs/source/nn.functional.rst'),
    (torch.fft, 'torch.fft', 'docs/source/fft.rst'),
    (torch.special, 'torch.special', 'docs/source/special.rst'),
    (torch.linalg, 'torch.linalg', 'docs/source/linalg.rst'),
    (torch, 'torch', 'docs/source/torch.rst'),
    (torch.Tensor, 'torch.Tensor', 'docs/source/tensors.rst'),
]

# torch.abs, Tensor.abs, Tensor.abs_ are all considered to be different


def get_public_overridable_apis(pytorch_root='/raid/rzou/pt/quick'):
    results = {}
    all_overridable_apis = set(torch.overrides.get_testing_overrides().keys())
    for module, module_name, src in public_docs:
        with open(f'{pytorch_root}/{src}') as f:
            lines = f.readlines()
        # APIs eitehr begin with 4 spaces or ".. autofunction::"
        api_lines1 = [line.strip() for line in lines if line.startswith(' ' * 4)]
        api_lines2 = [line.strip()[len('.. autofunction:: '):]
                      for line in lines if line.startswith('.. autofunction::')]
        lines = api_lines1 + api_lines2
        lines = [line[7:] if line.startswith('Tensor.') else line for line in lines]
        lines = [line for line in lines if hasattr(module, line)]
        for line in lines:
            api = getattr(module, line)
            if api in all_overridable_apis:
                results[f'{module_name}.{line}'] = api
    return results


denylist = {
    'torch.Tensor.data_ptr',
    'torch.Tensor.dim',
    'torch.Tensor.element_size',
    'torch.Tensor.backward',
    'torch.Tensor.as_strided',
    'torch.Tensor.register_hook',
    'torch.Tensor.record_stream',
    'torch.Tensor.qscheme',
    'torch.Tensor.ndimension',
    'torch.Tensor.smm',
    'torch.Tensor.sspaddmm',
    'torch.Tensor.retain_grad',
    'torch.Tensor.sparse_mask',
    'torch.Tensor.sparse_dim',
    'torch.Tensor.dense_dim',
    'torch.Tensor.values',
    'torch.Tensor.indices',
    'torch.Tensor.numel',
    'torch.Tensor.size',
    'torch.Tensor.nelement',
    'torch.Tensor.q_scale',
    'torch.Tensor.q_zero_point',
    'torch.Tensor.q_per_channel_scales',
    'torch.Tensor.q_per_channel_zero_points',
    'torch.Tensor.q_per_channel_axis',
    'torch.Tensor.int_repr',
    'torch.Tensor.to_sparse',
    'torch.Tensor.is_inference',
    'torch.Tensor.storage',
    'torch.Tensor.storage_type',
}


def get_method_only_ops_we_care_about():
    apis = get_public_overridable_apis()
    result = []
    for key, _ in apis.items():
        if not key.startswith('torch.Tensor'):
            continue
        if key in denylist:
            continue
        api = key.split('.')[2]
        # filter out in-place
        if api.endswith('_'):
            continue
        if f'torch.{api}' not in apis.keys():
            result.append(api)
    return result

# Deduplicates torch.abs and Tensor.abs


def get_public_overridable_ops():
    results = get_public_overridable_apis()
    cpy = copy.deepcopy(results)
    for key, _ in cpy.items():
        if not key.startswith('torch.Tensor'):
            continue
        api = key.split('.')[2]
        if f'torch.{api}' in results.keys():
            del results[key]
    return results


def get_public_overridable_outplace_ops():
    results = get_public_overridable_ops()
    cpy = copy.deepcopy(results)
    for key, _ in cpy.items():
        # NB: there are no dunder methods bcs we don't document those
        if key.endswith('_'):
            del results[key]
    return results


def get_public_overridable_outplace_we_care_about():
    results = get_public_overridable_outplace_ops()
    cpy = copy.deepcopy(results)
    for key, _ in cpy.items():
        # quantization
        if 'quant' in key or '.q_' in key:
            del results[key]

        # is_cpu, etc. It doesn't make sense to have OpInfos for these
        if '.is_' in key:
            del results[key]

        if key in denylist and key in results:
            del results[key]
    return results

# e.g. nn.functional.softmax


def get_op(dotted_name):
    names = dotted_name.split('.')
    mod = torch
    for name in names:
        if not hasattr(mod, name):
            return None
        mod = getattr(mod, name)
    return mod

# Maps function -> [OpInfo]


def get_ops_covered_by_opinfos():
    ops = {}

    def safe_append(dct, key, val):
        if key in dct:
            dct[key].append(val)
        else:
            dct[key] = [val]

    for opinfo in op_db:
        func_op = get_op(opinfo.name)
        if func_op:
            safe_append(ops, func_op, opinfo)
        if opinfo.method_variant:
            safe_append(ops, opinfo.method_variant, opinfo)
        if opinfo.inplace_variant:
            safe_append(ops, opinfo.inplace_variant, opinfo)
        for alias in opinfo.aliases:
            safe_append(ops, alias.op, opinfo)
    return ops


factory_fns = {
    'tensor', 'zeros', 'ones', 'randn', 'arange', 'rand', 'empty', 'randperm',
    'linspace', 'logspace', 'hann_window', 'full', 'eye', 'blackman_window',
    'barlett_window', 'randint', 'range', 'arange',
}


def get_top_ops(torch_threshold, nn_fn_threshold):
    denylist = set({
        # These are either not real "operators", factory functions
        # that trivially work, or not-documented ops.
        'load', 'no_grad', 'save', 'from_numpy',
        'manual_seed', 'set_grad_enabled',
        'set_default_tensor_type', 'set_num_threads',
        'set_printoptions', 'numel',
        'set_default_dtype', 'sparse_coo_tensor', 'set_rng_state',
        'get_rng_state', 'get_default_dtype', 'initial_seed',
        'get_num_threads', 'quantize_per_tensor',
        'hann_window', 'is_tensor', 'as_tensor',
        'equal', 'enable_grad', 'seed', 'is_storage',
        'is_floating_point', 'nn.functional.torch',
        'set_flush_denormal', 'set_num_interop_threads', 'dequantize',
        'get_num_interop_threads', 'nn.functional.math',
        'nn.functional.threshold_',
        'nn.functional.selu_',
        'nn.functional.elu_',
        'nn.functional.rrelu_',
        'nn.functional.leaky_relu_',
        'nn.functional.hardtanh_',
        'nn.functional.has_torch_function',
        'nn.functional.has_torch_function_unary',
        'nn.functional.has_torch_function_variadic',
        'nn.functional.handle_torch_function',
        'nn.functional.adaptive_max_pool1d_with_indices',
        'nn.functional.adaptive_max_pool2d_with_indices',
        'nn.functional.adaptive_max_pool3d_with_indices',
        'nn.functional.fractional_max_pool2d_with_indices',
        'nn.functional.fractional_max_pool3d_with_indices',
        'is_complex',
        'grad',
        'quantize_per_channel',
        'nn.functional.max_pool2d_with_indices',
        'nn.functional.max_pool3d_with_indices',
        'nn.functional.max_pool1d_with_indices',
        'nn.functional.celu_',
        'nn.functional.grad',
        'nn.functional.relu_',
        'nn.functional.boolean_dispatch',
        'nn.functional.assert_int_or_pair',
        'fft',  # is namespace
    })

    torch_ops = [op[0] for op in top_ops.top_torch]
    nn_fn_ops = [op[0] for op in top_ops.get_nn_functional_top_list()]
    torch_ops = [op for op in torch_ops if op not in denylist]
    nn_fn_ops = [op for op in nn_fn_ops if op not in denylist]

    ops = torch_ops[:torch_threshold] + nn_fn_ops[:nn_fn_threshold]
    return ops


def get_top_ops_not_covered_by_opinfo(torch_threshold=0, nn_fn_threshold=0):
    ops = get_top_ops(torch_threshold, nn_fn_threshold)

    ops_with_opinfo = []
    for op in op_db:
        ops_with_opinfo.append(op.name)
        ops_with_opinfo.extend([op.name for op in op.aliases])
    ops_with_opinfo = set(ops_with_opinfo)

    result = [op for op in ops if op not in ops_with_opinfo]
    result = [op for op in result if op not in denylist]
    result = [op for op in result if op not in factory_fns]
    return result


def get_covered_ops(ops_list, invert=False):
    ops_covered_by_opinfo = get_ops_covered_by_opinfos()
    overridable_outplace_ops = ops_list
    results = {}
    for key, op in overridable_outplace_ops.items():
        cond = op in ops_covered_by_opinfo
        if invert:
            cond = not cond
        if cond:
            results[key] = op
    return results


class Status(Enum):
    Correct = 0
    Fast = 1


tests = {
    'test_vmap_exhaustive',
    'test_op_has_batch_rule',
    'test_vjp',
    'test_vmapvjp',
    'test_vmapvjp_has_batch_rule',
    'test_jvp',
    'test_vmapjvp',
}


def get_statuses(for_subset=None, invert=False):
    overridable_outplace_we_care_about = get_public_overridable_outplace_we_care_about()
    if for_subset is not None:
        overridable_outplace_we_care_about = {
            k: v
            for k, v in overridable_outplace_we_care_about.items()
            # Removes "torch."
            if k[6:] in for_subset
        }
    op_to_opinfo = get_ops_covered_by_opinfos()
    result = {}
    _ = get_covered_ops(overridable_outplace_we_care_about)

    def get_covered_tests(op):
        opinfos = op_to_opinfo[op]
        result = copy.deepcopy(tests)
        for opinfo in opinfos:
            for decorator in opinfo.decorators:
                if not hasattr(decorator, 'test_name'):
                    continue
                if decorator.test_name in tests and decorator.test_name in result:
                    result.remove(decorator.test_name)
        return result

    def get_all_aliases(op):
        opinfos = op_to_opinfo[op]
        result = []
        for opinfo in opinfos:
            result.append(opinfo.name)
            result.extend(opinfo.aliases)
        return set(result)

    for name, op in get_covered_ops(overridable_outplace_we_care_about).items():
        successful_tests = get_covered_tests(op)
        failed_tests = tests - successful_tests
        result[name] = failed_tests if invert else successful_tests
    return result


def transpose_statuses(for_subset=None, invert=False):
    statuses = get_statuses(for_subset, invert=invert)
    result = {}
    for test in tests:
        result[test] = set({})
    for op, supported in statuses.items():
        for test in supported:
            result[test].add(op)
    return result


overridable_apis = get_public_overridable_apis()

overridable_ops = get_public_overridable_ops()

overridable_outplace_ops = get_public_overridable_outplace_ops()

overridable_outplace_we_care_about = get_public_overridable_outplace_we_care_about()

tested_overridable_outplace_ops = get_covered_ops(overridable_outplace_we_care_about)
untested_overridable_outplace_ops = get_covered_ops(overridable_outplace_we_care_about, invert=True)

# print("List of OpInfos we need:")
# for key in untested_overridable_outplace_ops.keys():
#     print(key)
# print("-" * 80)
# print("")

print(f'Overridable public APIs: {len(overridable_apis)}')
print(f'Overridable public ops: {len(overridable_ops)}')
print(f'Overridable public outplace ops: {len(overridable_outplace_ops)}')
print(f'Overridable public outplace ops we care about: {len(overridable_outplace_we_care_about)}')
print(f'OpInfo-tested overridable public outplace ops: {len(tested_overridable_outplace_ops)}')


statuses = transpose_statuses()
for test in tests:
    print(f'{test} coverage {len(statuses[test])}')

method_only_ops = get_method_only_ops_we_care_about()
# for op in method_only_ops:
#     print(f'    {op},')

top_ops_not_covered_by_opinfo = get_top_ops_not_covered_by_opinfo(100, 25)
print('=' * 80)
for op in top_ops_not_covered_by_opinfo:
    print(f'{op}, {top_ops.usage_count[op]}')

# print("top ops not covered by opinfo: ")
# top_ops_not_covered_by_opinfo = get_top_ops_not_covered_by_opinfo(200, 50)
# for op in top_ops_not_covered_by_opinfo:
#     print(f'{op}, {top_ops.usage_count[op]}')

# print("top ops not covered by opinfo: ")
# top_ops_not_covered_by_opinfo = get_top_ops_not_covered_by_opinfo(220, 92)
# for op in top_ops_not_covered_by_opinfo:
#    print(f'{op}, {top_ops.usage_count[op]}')

# print("top ops not covered by opinfo: ")
# top_ops_not_covered_by_opinfo = get_top_ops_not_covered_by_opinfo(999, 999)
# for op in top_ops_not_covered_by_opinfo:
#     print(f'{op}, {top_ops.usage_count[op]}')


def remove_from_set(parent, to_remove):
    for to_remove_elt in to_remove:
        if to_remove_elt in parent:
            parent.remove(to_remove_elt)


def print_coverage_info(th=100, nn=25):
    print('=' * 80)
    print(f"top {th}, {nn} coverage")
    statuses = transpose_statuses(get_top_ops(th, nn), invert=True)
    top_ops_not_covered_by_opinfo = get_top_ops_not_covered_by_opinfo(th, nn)

    # testing problems
    exemptions = {
        'torch.nn.functional.dropout',  # randomness
    }

    # Allowed exemptions
    vmap_exemptions = {
        'torch.randn_like',  # randomness
        'torch.rand_like',  # randomness
        'torch.allclose',  # number output
        'torch.unique',  # dynamic
        'torch.nonzero',  # dynamic
        'torch.masked_select',  # dynamic
        'torch.prod',  # dynamic (backward)
        'torch.norm',  # norm with nuc is not commonly used; we support the other cases.
        'torch.svd',  # There isn't a bug, it is just nondeterministic so we can't test it.
        'torch.nn.functional.embedding',  # We support everything except the sparse option.
    }
    remove_from_set(statuses['test_vmap_exhaustive'], vmap_exemptions)
    remove_from_set(statuses['test_vmapvjp'], vmap_exemptions)
    remove_from_set(statuses['test_vmapvjp_has_batch_rule'], vmap_exemptions)
    remove_from_set(statuses['test_op_has_batch_rule'], vmap_exemptions)
    remove_from_set(statuses['test_vmapjvp'], vmap_exemptions)
    for test in tests:
        remove_from_set(statuses[test], exemptions)

    print(f"total ops in set: {th + nn}")
    print(f"tested by OpInfo: {th + nn - len(top_ops_not_covered_by_opinfo)}")
    for test in tests:
        if test in {'test_jvp', 'test_vmapjvp'}:
            continue
        print(f'{test} failing coverage {len(statuses[test])}')

    # We don't care about these yet
    del statuses['test_jvp']
    del statuses['test_vmapjvp']

    pprint.pprint(statuses)


# print_coverage_info(100, 25)
# print_coverage_info(200, 50)

# pprint.pprint(get_top_ops(100, 25))
