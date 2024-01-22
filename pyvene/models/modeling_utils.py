import random, torch
import numpy as np
from torch import nn
from .intervenable_modelcard import *
from .interventions import *


def get_internal_model_type(model):
    """Return the model type."""
    return type(model)


def is_stateless(model):
    """Determine if the model is stateful (e.g., rnn) or stateless (e.g.,
    transformer)
    """
    if is_gru(model):
        return False
    return True


def is_gru(model):
    """Determine if this is a transformer model."""
    if (
        type(model) == GRUModel
        or type(model) == GRULMHeadModel
        or type(model) == GRUForClassification
    ):
        return True
    return False


def is_mlp(model):
    """Determine if this is a mlp model."""
    if type(model) == MLPModel or type(model) == MLPForClassification:
        return True
    return False


def is_transformer(model):
    """Determine if this is a transformer model."""
    if not is_gru(model) and not is_mlp(model):
        return True
    return False


def print_forward_hooks(main_module):
    """Function to print forward hooks of a module and its sub-modules."""
    for name, submodule in main_module.named_modules():
        if hasattr(submodule, "_forward_hooks") and submodule._forward_hooks:
            print(f"Module: {name if name else 'Main Module'}")
            for hook_id, hook in submodule._forward_hooks.items():
                print(f"  ID: {hook_id}, Hook: {hook}")

        if hasattr(submodule, "_forward_pre_hooks") and submodule._forward_hooks:
            print(f"Module: {name if name else 'Main Module'}")
            for hook_id, hook in submodule._forward_pre_hooks.items():
                print(f"  ID: {hook_id}, Hook: {hook}")


def remove_forward_hooks(main_module: nn.Module):
    """Function to remove all forward and pre-forward hooks from a module and

    its sub-modules.
    """

    # Remove forward hooks
    for _, submodule in main_module.named_modules():
        if hasattr(submodule, "_forward_hooks"):
            hooks = list(submodule._forward_hooks.keys())  # Get a list of hook IDs
            for hook_id in hooks:
                submodule._forward_hooks.pop(hook_id)

        # Remove pre-forward hooks
        if hasattr(submodule, "_forward_pre_hooks"):
            pre_hooks = list(
                submodule._forward_pre_hooks.keys()
            )  # Get a list of pre-hook IDs
            for pre_hook_id in pre_hooks:
                submodule._forward_pre_hooks.pop(pre_hook_id)


def getattr_for_torch_module(model, parameter_name):
    """Recursively fetch the model based on the name."""
    current_module = model
    for param in parameter_name.split("."):
        if "[" in param:
            current_module = getattr(current_module, param.split("[")[0])[
                int(param.split("[")[-1].strip("]"))
            ]
        else:
            current_module = getattr(current_module, param)
    return current_module


def get_dimension(model_type, model_config, representation) -> int:
    """Based on the representation, get the aligning dimension size."""

    dimension_proposals = type_to_dimension_mapping[model_type][
        representation.component
    ]
    for proposal in dimension_proposals:
        if "*" in proposal:
            # often constant multiplier with MLP
            dimension = getattr_for_torch_module(
                model_config, proposal.split("*")[0]
            ) * int(proposal.split("*")[1])
        elif "/" in proposal:
            # often split by head number
            dimension = int(
                getattr_for_torch_module(model_config, proposal.split("/")[0])
                / getattr_for_torch_module(model_config, proposal.split("/")[1])
            )
        else:
            dimension = getattr_for_torch_module(model_config, proposal)
        if dimension is not None:
            return dimension * int(representation.max_number_of_units)

    assert False


def get_representation_dimension_by_type(
    model_type, model_config, representation_type
) -> int:
    """Based on the representation, get the aligning dimension size."""

    dimension_proposals = type_to_dimension_mapping[model_type][representation_type]
    for proposal in dimension_proposals:
        if "*" in proposal:
            # often constant multiplier with MLP
            dimension = getattr_for_torch_module(
                model_config, proposal.split("*")[0]
            ) * int(proposal.split("*")[1])
        elif "/" in proposal:
            # often split by head number
            dimension = int(
                getattr_for_torch_module(model_config, proposal.split("/")[0])
                / getattr_for_torch_module(model_config, proposal.split("/")[1])
            )
        else:
            dimension = getattr_for_torch_module(model_config, proposal)
        if dimension is not None:
            return dimension

    assert False


def get_module_hook(model, representation) -> nn.Module:
    """Render the intervening module with a hook."""
    type_info = type_to_module_mapping[get_internal_model_type(model)][
        representation.component
    ]
    parameter_name = type_info[0]
    hook_type = type_info[1]
    if "%s" in parameter_name and representation.moe_key is None:
        # we assume it is for the layer.
        parameter_name = parameter_name % (representation.layer)
    else:
        parameter_name = parameter_name % (
            int(representation.layer),
            int(representation.moe_key),
        )
    module = getattr_for_torch_module(model, parameter_name)
    module_hook = getattr(module, hook_type)

    return module_hook


def check_sorted_intervenables_by_topological_order(
    model, representations, sorted_keys
):
    """Sort the intervention with topology in transformer arch."""
    if is_transformer(model):
        TOPOLOGICAL_ORDER = CONST_TRANSFORMER_TOPOLOGICAL_ORDER
    elif is_mlp(model):
        TOPOLOGICAL_ORDER = CONST_MLP_TOPOLOGICAL_ORDER
    elif is_gru(model):
        TOPOLOGICAL_ORDER = CONST_GRU_TOPOLOGICAL_ORDER

    scores = {}
    for k, _ in representations.items():
        l = 100 * (int(k.split(".")[1]) + 1)
        r = 10 * TOPOLOGICAL_ORDER.index(k.split(".")[3])
        # incoming order in case they are ordered
        o = 1 * (int(k.split("#")[1]) + 1)
        scores[k] = l + r + o
    sorted_keys_by_topological_order = sorted(scores.keys(), key=lambda x: scores[x])

    return sorted_keys == sorted_keys_by_topological_order


class HandlerList:
    """General class to set hooks and set off hooks."""

    def __init__(self, handlers):
        self.handlers = handlers

    def __len__(self):
        return len(self.handlers)

    def remove(self):
        for handler in self.handlers:
            handler.remove()

    def extend(self, new_handlers):
        self.handlers.extend(new_handlers.handlers)
        return self


def bsd_to_b_sd(tensor):
    """Convert a tensor of shape (b, s, d) to (b, s*d)."""
    if tensor is None:
        return tensor
    b, s, d = tensor.shape
    return tensor.reshape(b, s * d)


def b_sd_to_bsd(tensor, s):
    """Convert a tensor of shape (b, s*d) back to (b, s, d)."""
    if tensor is None:
        return tensor
    b, sd = tensor.shape
    d = sd // s
    return tensor.reshape(b, s, d)


def bhsd_to_bs_hd(tensor):
    """Convert a tensor of shape (b, h, s, d) to (b, s, h*d)."""
    if tensor is None:
        return tensor
    b, h, s, d = tensor.shape
    return tensor.permute(0, 2, 1, 3).reshape(b, s, h * d)


def bs_hd_to_bhsd(tensor, h):
    """Convert a tensor of shape (b, s, h*d) back to (b, h, s, d)."""
    if tensor is None:
        return tensor
    b, s, hd = tensor.shape

    d = hd // h

    return tensor.reshape(b, s, h, d).permute(0, 2, 1, 3)


def gather_neurons(tensor_input, unit, unit_locations_as_list):
    """Gather intervening neurons.

    :param tensor_input: tensors of shape (batch_size, sequence_length, ...) if
    `unit` is "pos" or "h", tensors of shape (batch_size, num_heads,
    sequence_length, ...) if `unit` is "h.pos"
    :param unit: the intervention units to gather. Units could be "h" - head
    number, "pos" - position in the sequence, or "dim" - a particular dimension in
    the embedding space. If intervening multiple units, they are ordered and
    separated by `.`. Currently only support "pos", "h", and "h.pos" units.
    :param unit_locations_as_list: tuple of lists of lists of positions to gather
    in tensor_input, according to the unit.
    :return the gathered tensor as tensor_output
    """
    if "." in unit:
        unit_locations = (
            torch.tensor(unit_locations_as_list[0], device=tensor_input.device),
            torch.tensor(unit_locations_as_list[1], device=tensor_input.device),
        )
    else:
        unit_locations = torch.tensor(
            unit_locations_as_list, device=tensor_input.device
        )

    if unit in {"pos", "h"}:
        tensor_output = torch.gather(
            tensor_input,
            1,
            unit_locations.reshape(
                *unit_locations.shape, *(1,) * (len(tensor_input.shape) - 2)
            ).expand(-1, -1, *tensor_input.shape[2:]),
        )

        return tensor_output
    elif unit in {"h.pos"}:
        # we assume unit_locations is a tuple
        head_unit_locations = unit_locations[0]
        pos_unit_locations = unit_locations[1]

        head_tensor_output = torch.gather(
            tensor_input,
            1,
            head_unit_locations.reshape(
                *head_unit_locations.shape, *(1,) * (len(tensor_input.shape) - 2)
            ).expand(-1, -1, *tensor_input.shape[2:]),
        )  # b, h, s, d
        d = head_tensor_output.shape[1]
        pos_tensor_input = bhsd_to_bs_hd(head_tensor_output)
        pos_tensor_output = torch.gather(
            pos_tensor_input,
            1,
            pos_unit_locations.reshape(
                *pos_unit_locations.shape, *(1,) * (len(pos_tensor_input.shape) - 2)
            ).expand(-1, -1, *pos_tensor_input.shape[2:]),
        )  # b, num_unit (pos), num_unit (h)*d
        tensor_output = bs_hd_to_bhsd(pos_tensor_output, d)

        return tensor_output  # b, num_unit (h), num_unit (pos), d
    elif unit in {"t"}:
        # for stateful models, intervention location is guarded outside gather
        return tensor_input
    elif unit in {"dim", "pos.dim", "h.dim", "h.pos.dim"}:
        assert False, f"Not Implemented Gathering with Unit = {unit}"


def split_heads(tensor, num_heads, attn_head_size):
    """Splits hidden_size dim into attn_head_size and num_heads."""
    new_shape = tensor.size()[:-1] + (num_heads, attn_head_size)
    tensor = tensor.view(new_shape)
    return tensor.permute(0, 2, 1, 3)  # (batch, head, seq_length, head_features)


def output_to_subcomponent(output, representation_type, model_type, model_config):
    if "head" in representation_type or representation_type in {
        "query_output",
        "key_output",
        "value_output",
    }:
        n_embd = get_representation_dimension_by_type(
            model_type, model_config, "block_output"
        )
        attn_head_size = get_representation_dimension_by_type(
            model_type, model_config, "head_attention_value_output"
        )
        num_heads = int(n_embd / attn_head_size)
    else:
        pass  # this is likely to be non-transformer model for advance usages

    # special handling when QKV are not separated by the model
    if model_type in {
        hf_models.gpt2.modeling_gpt2.GPT2Model,
        hf_models.gpt2.modeling_gpt2.GPT2LMHeadModel,
    }:
        if representation_type in {
            "query_output",
            "key_output",
            "value_output",
            "head_query_output",
            "head_key_output",
            "head_value_output",
        }:
            qkv = output.split(n_embd, dim=2)
            if representation_type in {
                "head_query_output",
                "head_key_output",
                "head_value_output",
            }:
                qkv = (
                    split_heads(qkv[0], num_heads, attn_head_size),
                    split_heads(qkv[1], num_heads, attn_head_size),
                    split_heads(qkv[2], num_heads, attn_head_size),
                )  # each with (batch, head, seq_length, head_features)
            return qkv[CONST_QKV_INDICES[representation_type]]
        elif representation_type in {"head_attention_value_output"}:
            return split_heads(output, num_heads, attn_head_size)
        else:
            return output
    elif model_type in {GRUModel, GRULMHeadModel, GRUForClassification}:
        if representation_type in {
            "reset_x2h_output",
            "new_x2h_output",
            "reset_h2h_output",
            "reset_h2h_output",
            "update_h2h_output",
            "new_h2h_output",
        }:
            n_embd = get_representation_dimension_by_type(
                model_type, model_config, "cell_output"
            )
            start_index = CONST_RUN_INDICES[representation_type] * n_embd
            end_index = (CONST_RUN_INDICES[representation_type] + 1) * n_embd
            return output[..., start_index:end_index]
        else:
            return output
    else:
        if representation_type in {
            "head_query_output",
            "head_key_output",
            "head_value_output",
            "head_attention_value_output",
        }:
            return split_heads(output, num_heads, attn_head_size)
        else:
            return output


def scatter_neurons(
    tensor_input,
    replacing_tensor_input,
    representation_type,
    unit,
    unit_locations_as_list,
    model_type,
    model_config,
    use_fast,
):
    """Replace selected neurons in `tensor_input` by `replacing_tensor_input`.

    :param tensor_input: tensors of shape (batch_size, sequence_length, ...) if
    `unit` is "pos" or "h", tensors of shape (batch_size, num_heads,
    sequence_length, ...) if `unit` is "h.pos"
    :param replacing_tensor_input: tensors of shape (batch_size, sequence_length,
    ...) if `unit` is "pos" or
    "h", tensors of shape (batch_size, num_heads, sequence_length, ...) if
    `unit` is "h.pos".
    :param representation_type: types of intervention representations, such as
    "block_output" and "query_output"
    :param unit: the intervention units to gather. Units could be "h" - head
    number, "pos" - position in the sequence, or "dim" - a particular dimension in
    the embedding space. If intervening multiple units, they are ordered and
    separated by `.`. Currently only support "pos", "h", and "h.pos" units.
    :param unit_locations_as_list: tuple of lists of lists of positions to gather
    in tensor_input, according to the unit.
    :param model_type: Hugging Face Model Type
    :param model_config: Hugging Face Model Config
    :param use_fast: whether to use fast path (TODO: fast path condition)
    :return the in-place modified tensor_input
    """
    if "." in unit:
        # extra dimension for multi-level intervention
        unit_locations = (
            torch.tensor(unit_locations_as_list[0], device=tensor_input.device),
            torch.tensor(unit_locations_as_list[1], device=tensor_input.device),
        )
    else:
        unit_locations = torch.tensor(
            unit_locations_as_list, device=tensor_input.device
        )

    if "head" in representation_type or representation_type in {
        "query_output",
        "key_output",
        "value_output",
    }:
        n_embd = get_representation_dimension_by_type(
            model_type, model_config, "block_output"
        )
        attn_head_size = get_representation_dimension_by_type(
            model_type, model_config, "head_attention_value_output"
        )
        num_heads = int(n_embd / attn_head_size)
    else:
        pass  # this is likely to be non-transformer model for advance usages

    # special handling when QKV are not separated by the model.
    if model_type in {
        hf_models.gpt2.modeling_gpt2.GPT2Model,
        hf_models.gpt2.modeling_gpt2.GPT2LMHeadModel,
    }:
        if (
            "query" in representation_type
            or "key" in representation_type
            or "value" in representation_type
        ) and "attention" not in representation_type:
            start_index = CONST_QKV_INDICES[representation_type] * n_embd
            end_index = (CONST_QKV_INDICES[representation_type] + 1) * n_embd
        else:
            start_index, end_index = None, None
    elif model_type in {GRUModel, GRULMHeadModel, GRUForClassification}:
        if representation_type in {
            "reset_x2h_output",
            "new_x2h_output",
            "reset_h2h_output",
            "reset_h2h_output",
            "update_h2h_output",
            "new_h2h_output",
        }:
            n_embd = get_representation_dimension_by_type(
                model_type, model_config, "cell_output"
            )
            start_index = CONST_RUN_INDICES[representation_type] * n_embd
            end_index = (CONST_RUN_INDICES[representation_type] + 1) * n_embd
        else:
            start_index, end_index = None, None
    else:
        start_index, end_index = None, None

    if unit == "t":
        # time series models, e.g., gru
        for batch_i, _ in enumerate(unit_locations):
            tensor_input[batch_i, start_index:end_index] = replacing_tensor_input[
                batch_i
            ]
    else:
        if "head" in representation_type:
            start_index = 0 if start_index is None else start_index
            end_index = 0 if end_index is None else end_index
            # head-based scattering
            if unit in {"h.pos"}:
                # we assume unit_locations is a tuple
                for head_batch_i, head_locations in enumerate(unit_locations[0]):
                    for head_loc_i, head_loc in enumerate(head_locations):
                        for pos_loc_i, pos_loc in enumerate(
                            unit_locations[1][head_batch_i]
                        ):
                            h_start_index = start_index + head_loc * attn_head_size
                            h_end_index = start_index + (head_loc + 1) * attn_head_size
                            tensor_input[
                                head_batch_i, pos_loc, h_start_index:h_end_index
                            ] = replacing_tensor_input[
                                head_batch_i, head_loc_i, pos_loc_i
                            ]  # [dh]
            else:
                for batch_i, locations in enumerate(unit_locations):
                    for loc_i, loc in enumerate(locations):
                        h_start_index = start_index + loc * attn_head_size
                        h_end_index = start_index + (loc + 1) * attn_head_size
                        tensor_input[
                            batch_i, :, h_start_index:h_end_index
                        ] = replacing_tensor_input[
                            batch_i, loc_i
                        ]  # [s, dh]
        else:
            if use_fast:
                tensor_input[
                    :, unit_locations[0], start_index:end_index
                ] = replacing_tensor_input[:]
            else:
                # pos-based scattering
                for batch_i, locations in enumerate(unit_locations):
                    tensor_input[
                        batch_i, locations, start_index:end_index
                    ] = replacing_tensor_input[batch_i]
    return tensor_input


def do_intervention(
    base_representation, source_representation, intervention, subspaces
):
    """Do the actual intervention."""

    num_unit = base_representation.shape[1]

    # flatten
    original_base_shape = base_representation.shape
    if len(original_base_shape) == 2 or (
        isinstance(intervention, LocalistRepresentationIntervention)
    ):
        # no pos dimension, e.g., gru
        base_representation_f = base_representation
        source_representation_f = source_representation
    elif len(original_base_shape) == 3:
        # b, num_unit (pos), d -> b, num_unit*d
        base_representation_f = bsd_to_b_sd(base_representation)
        source_representation_f = bsd_to_b_sd(source_representation)
    elif len(original_base_shape) == 4:
        # b, num_unit (h), s, d -> b, s, num_unit*d
        base_representation_f = bhsd_to_bs_hd(base_representation)
        source_representation_f = bhsd_to_bs_hd(source_representation)
    else:
        assert False  # what's going on?

    intervened_representation = intervention(
        base_representation_f, source_representation_f, subspaces
    )

    post_d = intervened_representation.shape[-1]

    # unflatten
    if len(original_base_shape) == 2 or isinstance(
        intervention, LocalistRepresentationIntervention
    ):
        # no pos dimension, e.g., gru
        pass
    elif len(original_base_shape) == 3:
        intervened_representation = b_sd_to_bsd(intervened_representation, num_unit)
    elif len(original_base_shape) == 4:
        intervened_representation = bs_hd_to_bhsd(intervened_representation, num_unit)
    else:
        assert False  # what's going on?

    return intervened_representation


def simple_output_to_subcomponent(output, representation_type, model_config):
    """This is an oversimplied version for demo."""
    return output


def simple_scatter_intervention_output(
    original_output,
    intervened_representation,
    representation_type,
    unit,
    unit_locations,
    model_config,
):
    """This is an oversimplied version for demo."""
    for batch_i, locations in enumerate(unit_locations):
        original_output[batch_i, locations] = intervened_representation[batch_i]


def weighted_average(values, weights):
    if len(values) != len(weights):
        raise ValueError("The length of values and weights must be the same.")

    total = sum(v * w for v, w in zip(values, weights))
    return total / sum(weights)
