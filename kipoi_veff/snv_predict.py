import copy
import itertools
import logging
import os
import tempfile

import numpy as np
import pandas as pd
import six
from tqdm import tqdm

import kipoi_veff
from kipoi_veff.scores import Logit, get_scoring_fns
from kipoi_veff.utils import select_from_dl_batch, OutputReshaper, default_vcf_id_gen, \
    ModelInfoExtractor, BedWriter, VariantLocalisation, ensure_tabixed_vcf
from kipoi_veff.utils.io import VcfWriter
from .utils import is_indel_wrapper
from kipoi.utils import cd

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def analyse_model_preds(model, ref, alt, diff_types,
                        output_reshaper, output_filter=None, ref_rc=None, alt_rc=None, **kwargs):
    seqs = {"ref": ref, "alt": alt}
    if ref_rc is not None:
        seqs["ref_rc"] = ref_rc
    if alt_rc is not None:
        seqs["alt_rc"] = alt_rc
    if not isinstance(diff_types, dict):
        raise Exception("diff_types has to be a dictionary of callables. Keys will be used to annotate output.")
    # This is deprecated as no simple deduction of sequence length is possible anymore
    # assert np.all([np.array(_get_seq_len(ref)) == np.array(_get_seq_len(seqs[k])) for k in seqs.keys() if k != "ref"])

    # Make predictions
    preds = {}
    out_annotation = None
    for k in seqs:
        # Flatten the model output
        with cd(model.source_dir):
            preds_out, pred_labels = output_reshaper.flatten(model.predict_on_batch(seqs[k]))
        if out_annotation is None:
            out_annotation = pred_labels
            # determine which outputs should be selected
            if output_filter is None:
                output_filter = np.zeros(pred_labels.shape[0]) == 0
            else:
                if isinstance(output_filter, six.string_types) or isinstance(output_filter, int):
                    output_filter = np.array([output_filter])
                elif isinstance(output_filter, list):
                    output_filter = np.array(output_filter)
                # Make sure it is a boolean filter of the right shape
                if output_filter.dtype == bool:
                    assert (output_filter.shape == out_annotation.shape)
                # Numerical index?
                elif np.issubdtype(output_filter.dtype, np.number):
                    assert np.max(output_filter) <= out_annotation.shape[0]
                    output_filter = np.in1d(np.arange(out_annotation.shape[0]), output_filter)
                # Assumed that string output label
                else:
                    assert np.all(np.in1d(output_filter, out_annotation))
                    output_filter = np.in1d(out_annotation, output_filter)
        # Filter outputs if required
        preds[k] = np.array(preds_out[..., output_filter])

    # Run the analysis callables
    outputs = {}
    for k in diff_types:
        outputs[k] = pd.DataFrame(diff_types[k](**preds), columns=out_annotation[output_filter])

    return outputs


def homogenise_seqname(query_seqname, possible_seqnames):
    possible_seqnames_stripped = [el.replace("chr", "") for el in possible_seqnames]
    query_seqname_stripped = query_seqname.replace("chr", "")
    if query_seqname not in possible_seqnames:
        if len(set(possible_seqnames_stripped)) != len(set(possible_seqnames)):
            raise Exception("Seqnames are not unique after removing \"chr\" prefix.")
        no_prefix = set(possible_seqnames_stripped) == set(possible_seqnames)
        if query_seqname_stripped in possible_seqnames_stripped:
            if no_prefix:
                return query_seqname_stripped
            else:
                return "chr" + query_seqname_stripped
    else:
        return query_seqname


def _overlap_vcf_region(vcf_obj, regions, exclude_indels=True):
    """
    Overlap a vcf with regions generated by the dataloader
    The region definition is assumed to be 0-based hence it is converted to 1-based for tabix overlaps!
    Returns VCF records
    """
    assert isinstance(regions["chr"], list) or isinstance(regions["chr"], np.ndarray)
    contained_regions = []
    vcf_records = []
    for i in range(len(regions["chr"])):
        chr_label = homogenise_seqname(regions["chr"][i], vcf_obj.seqnames)
        chrom, start, end = chr_label, regions["start"][i] + 1, regions["end"][i]
        region_str = "{0}:{1}-{2}".format(chrom, start, end)
        variants = vcf_obj(region_str)
        for record in variants:
            if is_indel_wrapper(record) and exclude_indels:
                continue
            vcf_records.append(record)
            contained_regions.append(i)
    #
    return vcf_records, contained_regions


# For every post-processing-activated DNA output assign a sequence mutator + the name of the metadata ranges name
# In _generate_seq_sets collect all the regions that might overlap. Make an object with mutatability:
# [{vcf_record, region, sample_within_batch_id, affected_seqeunce_output_name},...]

def merge_intervals_strandaware(ranges_dict):
    """
    Perform in-silico mutagenesis on what the dataloader has returned.

    This function has to convert the DNA regions in the model input according to ref, alt, fwd, rc and
    return a dictionary of which the keys are compliant with evaluation_function arguments

    DataLoaders that implement fwd and rc sequence output *__at once__* are not treated in any special way.
    Perform in-silico mutagenesis on what the dataloader has returned.

    This function has to convert the DNA regions in the model input according to ref, alt, fwd, rc and
    return a dictionary of which the keys are compliant with evaluation_function arguments

    DataLoaders that implement fwd and rc sequence output *__at once__* are not treated in any special way.
    `ranges_dict`: dictionary of GenomicsRanges
    Returns unified ranges
    """
    from intervaltree import IntervalTree
    chrom_trees = {}
    for k in ranges_dict:
        assert len(ranges_dict[k]["chr"]) == 1
        chr_str = [ranges_dict[k]["chr"][0], ranges_dict[k]["strand"][0]]
        start = ranges_dict[k]["start"][0]
        end = ranges_dict[k]["end"][0]
        if chr_str not in chrom_trees:
            chrom_trees[chr_str] = IntervalTree()
        # append new region to the interval tree
        chrom_trees[chr_str][start:end] = [k]
    # merge overlapping regions and append the metadata fields
    [chrom_trees[chr_str].merge_overlaps(lambda x, y: x + y) for chr_str in chrom_trees]
    out_regions = {k: [] for k in ["chr", "start", "end", "strand"]}
    ranges_ks = []
    for chr_str in chrom_trees:
        for interval in chrom_trees[chr_str]:
            out_regions["chr"].append(chr_str[0])
            out_regions["strand"].append(chr_str[1])
            out_regions["start"].append(interval.begin)
            out_regions["end"].append(interval.end)
            ranges_ks.append(interval.data)
    return out_regions, ranges_ks


def merge_intervals(ranges_dict):
    """
    `ranges_dict`: dictionary of GenomicsRanges
    Returns unified ranges
    """
    from intervaltree import IntervalTree
    chrom_trees = {}
    for k in ranges_dict:
        assert len(ranges_dict[k]["chr"]) == 1
        chr = ranges_dict[k]["chr"][0]
        start = ranges_dict[k]["start"][0]
        end = ranges_dict[k]["end"][0]
        if chr not in chrom_trees:
            chrom_trees[chr] = IntervalTree()
        # append new region to the interval tree
        chrom_trees[chr][start:end] = [k]

    # merge overlapping regions and append the metadata fields
    [chrom_trees[chr].merge_overlaps(lambda x, y: x + y) for chr in chrom_trees]

    # convert back to GenomicRanges-compliant dictionaries
    out_regions = {k: [] for k in ["chr", "start", "end"]}
    ranges_ks = []
    for chr in chrom_trees:
        for interval in chrom_trees[chr]:
            out_regions["chr"].append(chr)
            out_regions["start"].append(interval.begin)
            out_regions["end"].append(interval.end)
            ranges_ks.append(interval.data)

    out_regions["strand"] = ["*"] * len(out_regions["chr"])
    return out_regions, ranges_ks


def get_genomicranges_line(gr_obj, i):
    return {k: v[i:(i + 1)] for k, v in gr_obj.items()}


def get_variants_in_regions_search_vcf(dl_batch, seq_to_meta, vcf_fh):
    """
    Function that overlaps metadata ranges with a vcf by merging the regions. When regions are party overlapping then
    a variant will be tagged with all sequence-fields that participated in the merged region, hence not all input
    regions might be affected by the variant.
    """
    vcf_records = []  # list of vcf records to use
    process_lines = []  # sample id within batch
    process_seq_fields = []  # sequence fields that should be mutated
    #
    meta_to_seq = {v: [k for k in seq_to_meta if seq_to_meta[k] == v] for v in seq_to_meta.values()}
    all_meta_fields = list(set(seq_to_meta.values()))
    #
    num_samples_in_batch = len(dl_batch['metadata'][all_meta_fields[0]]["chr"])
    #
    # If we should search for the overlapping VCF lines - for every sample collect all region objects
    # under the assumption that all generated sequences have the same number of samples in a batch:
    for line_id in range(num_samples_in_batch):
        # check is there is more than one metadata_field that is used:
        if len(all_meta_fields) > 1:
            # one region per meta_field
            regions_by_meta = {k: get_genomicranges_line(dl_batch['metadata'][k], line_id)
                               for k in all_meta_fields}
            # regions_unif: union across all regions. meta_field_unif_r: meta_fields, has the length of regions_unif
            regions_unif, meta_field_unif_r = merge_intervals(regions_by_meta)
        else:
            # Only one meta_field and only one line hence:
            meta_field_unif_r = [all_meta_fields]
            # Only one region:
            regions_unif = get_genomicranges_line(dl_batch['metadata'][all_meta_fields[0]], line_id)
        #
        vcf_records_here, process_lines_rel = _overlap_vcf_region(vcf_fh, regions_unif)
        #
        for rec, sub_line_id in zip(vcf_records_here, process_lines_rel):
            vcf_records.append(rec)
            process_lines.append(line_id)
            metas = []
            for f in meta_field_unif_r[sub_line_id]:
                metas += meta_to_seq[f]
            process_seq_fields.append(metas)
    return vcf_records, process_lines, process_seq_fields


def get_variants_in_regions_sequential_vcf(dl_batch, seq_to_meta, vcf_fh, vcf_id_generator_fn):
    vcf_records = []  # list of vcf records to use
    process_ids = []  # id from genomic ranges metadata
    process_lines = []  # sample id within batch
    process_seq_fields = []  # sequence fields that should be mutated

    all_mut_seq_fields = list(set(seq_to_meta.keys()))
    all_meta_fields = list(set(seq_to_meta.values()))

    ranges = None
    for meta_field in all_meta_fields:
        rng = dl_batch['metadata'][meta_field]
        if ranges is None:
            ranges = rng
        else:
            # for k in ["chr", "start", "end", "id"]:
            for k in ["id"]:
                assert np.all(ranges[k] == rng[k])

    # Now continue going sequentially through the vcf assigning vcf record with regions to test.
    for i, returned_id in enumerate(ranges["id"]):
        for record in vcf_fh:
            id = vcf_id_generator_fn(record)
            if str(id) == str(returned_id):
                vcf_records.append(record)
                process_ids.append(returned_id)
                process_lines.append(i)
                process_seq_fields.append(all_mut_seq_fields)
                break
            else:
                # Warn here...
                logger.warn("Skipping VCF line (%s) because generated region is for different variant.." % str(id))
                pass
    return vcf_records, process_lines, process_seq_fields, process_ids


def get_variants_df(seq_key, ranges_input_obj, vcf_records, process_lines, process_ids, process_seq_fields):
    preproc_conv = {"pp_line": [], "varpos_rel": [], "ref": [], "alt": [], "start": [], "end": [], "id": [],
                    "do_mutate": []}

    if ("strand" in ranges_input_obj) and (isinstance(ranges_input_obj["strand"], list) or
                                           isinstance(ranges_input_obj["strand"], np.ndarray)):
        preproc_conv["strand"] = []

    for i, record in enumerate(vcf_records):
        assert not is_indel_wrapper(record)  # Catch indels, that needs a slightly modified processing
        ranges_input_i = process_lines[i]
        new_vals = {k: np.nan for k in preproc_conv.keys() if k not in ["do_mutate", "pp_line"]}
        new_vals["do_mutate"] = False
        new_vals["pp_line"] = i
        new_vals["id"] = str(process_ids[i])
        if seq_key in process_seq_fields[i]:
            pre_new_vals = {}
            pre_new_vals["start"] = ranges_input_obj["start"][ranges_input_i] + 1
            pre_new_vals["end"] = ranges_input_obj["end"][ranges_input_i]
            pre_new_vals["varpos_rel"] = int(record.POS) - pre_new_vals["start"]
            if not ((pre_new_vals["varpos_rel"] < 0) or
                    (pre_new_vals["varpos_rel"] > (pre_new_vals["end"] - pre_new_vals["start"] + 1))):

                # If variant lies in the region then continue
                pre_new_vals["do_mutate"] = True
                pre_new_vals["ref"] = str(record.REF)
                pre_new_vals["alt"] = str(record.ALT[0])

                if "strand" in preproc_conv:
                    pre_new_vals["strand"] = ranges_input_obj["strand"][ranges_input_i]

                # overwrite the nans with actual data now that
                for k in pre_new_vals:
                    new_vals[k] = pre_new_vals[k]

        for k in new_vals:
            preproc_conv[k].append(new_vals[k])

    # If strand wasn't a list then try to still fix it..
    if "strand" not in preproc_conv:
        if "strand" not in ranges_input_obj:
            preproc_conv["strand"] = ["*"] * len(preproc_conv["pp_line"])
        elif isinstance(ranges_input_obj["strand"], six.string_types):
            preproc_conv["strand"] = [ranges_input_obj["strand"]] * len(preproc_conv["pp_line"])
        else:
            raise Exception("Strand defintion invalid in metadata returned by dataloader.")

    preproc_conv_df = pd.DataFrame(preproc_conv)
    return preproc_conv_df


class SampleCounter():

    def __init__(self):
        self.sample_it_counter = 0

    def get_ids(self, number=1):
        ret = [str(i) for i in range(self.sample_it_counter, self.sample_it_counter + number)]
        self.sample_it_counter += number
        return ret


def _generate_seq_sets(dl_ouput_schema, dl_batch, vcf_fh, vcf_id_generator_fn, seq_to_mut, seq_to_meta,
                       sample_counter, vcf_search_regions=False, generate_rc=True):
    """
        Perform in-silico mutagenesis on what the dataloader has returned.

        This function has to convert the DNA regions in the model input according to ref, alt, fwd, rc and
        return a dictionary of which the keys are compliant with evaluation_function arguments

        DataLoaders that implement fwd and rc sequence output *__at once__* are not treated in any special way.

        Arguments:
        `dataloader`: dataloader object
        `dl_batch`: model input as generated by the datalaoder
        `vcf_fh`: cyvcf2 file handle
        `vcf_id_generator_fn`: function that generates ids for VCF records
        `seq_to_mut`: dictionary that contains DNAMutator classes with seq_fields as keys
        `seq_to_meta`: dictionary that contains Metadata key names with seq_fields as keys
        `vcf_search_regions`: if `False` assume that the regions are labelled and only test variants/region combinations for
        which the label fits. If `True` find all variants overlapping with all regions and test all.
        `generate_rc`: generate also reverse complement sequences. Only makes sense if supported by model.
        """

    all_meta_fields = list(set(seq_to_meta.values()))

    num_samples_in_batch = len(dl_batch['metadata'][all_meta_fields[0]]["chr"])

    metadata_ids = sample_counter.get_ids(num_samples_in_batch)

    if "_id" in dl_batch['metadata']:
        metadata_ids = dl_batch['metadata']['id']
        assert num_samples_in_batch == len(metadata_ids)

    # now get the right region from the vcf:
    # list of vcf records to use: vcf_records
    process_ids = None  # id from genomic ranges metadata: process_lines
    # sample id within batch: process_lines
    # sequence fields that should be mutated: process_seq_fields

    if vcf_search_regions:
        vcf_records, process_lines, process_seq_fields = get_variants_in_regions_search_vcf(dl_batch, seq_to_meta,
                                                                                            vcf_fh)
    else:
        # vcf_search_regions == False means: rely completely on the variant id
        # so for every sample assert that all metadata ranges ids agree and then find the entry.
        vcf_records, process_lines, process_seq_fields, process_ids = get_variants_in_regions_sequential_vcf(dl_batch,
                                                                                                             seq_to_meta,
                                                                                                             vcf_fh,
                                                                                                             vcf_id_generator_fn)

    # short-cut if no sequences are left
    if len(process_lines) == 0:
        return None

    if process_ids is None:
        process_ids = []
        for line_id in process_lines:
            process_ids.append(metadata_ids[line_id])

    # Generate 4 copies of the input set. subset datapoints if needed.
    input_set = {}
    seq_dirs = ["fwd"]
    if generate_rc:
        seq_dirs = ["fwd", "rc"]
    for s_dir, allele in itertools.product(seq_dirs, ["ref", "alt"]):
        k = "%s_%s" % (s_dir, allele)
        ds = dl_batch['inputs']
        all_lines = list(range(num_samples_in_batch))
        if process_lines != all_lines:
            # subset or rearrange elements
            ds = select_from_dl_batch(dl_batch['inputs'], process_lines, num_samples_in_batch)
        input_set[k] = copy.deepcopy(ds)

    # input_set matrices now are in the order required for mutation

    all_mut_seq_keys = list(set(itertools.chain.from_iterable(process_seq_fields)))

    # Start from the sequence inputs mentioned in the model.yaml
    for seq_key in all_mut_seq_keys:
        ranges_input_obj = dl_batch['metadata'][seq_to_meta[seq_key]]
        #
        # Assemble variant modification information
        # variants_df = get_variants_df(seq_key, ranges_input_obj, vcf_records,
        #                              process_lines, process_ids, process_seq_fields)

        vl = VariantLocalisation()
        vl.append_multi(seq_key, ranges_input_obj, vcf_records,
                        process_lines, process_ids, process_seq_fields)

        # for the individual sequence input key get the correct sequence mutator callable
        dna_mutator = seq_to_mut[seq_key]

        # Actually modify sequences according to annotation
        # two for loops
        for s_dir, allele in itertools.product(seq_dirs, ["ref", "alt"]):
            k = "%s_%s" % (s_dir, allele)
            if isinstance(dl_ouput_schema.inputs, dict):
                if seq_key not in input_set[k]:
                    raise Exception("Sequence field %s is missing in DataLoader output!" % seq_key)
                input_set[k][seq_key] = dna_mutator(input_set[k][seq_key], vl, allele, s_dir)
            elif isinstance(dl_ouput_schema.inputs, list):
                modified_set = []
                for seq_el, input_schema_el in zip(input_set[k], dl_ouput_schema.inputs):
                    if input_schema_el.name == seq_key:
                        modified_set.append(dna_mutator(seq_el, vl, allele, s_dir))
                    else:
                        modified_set.append(seq_el)
                input_set[k] = modified_set
            else:
                input_set[k] = dna_mutator(input_set[k], vl, allele, s_dir)

    #
    # Reformat so that effect prediction function will get its required inputs
    pred_set = {"ref": input_set["fwd_ref"], "alt": input_set["fwd_alt"]}
    if generate_rc:
        pred_set["ref_rc"] = input_set["rc_ref"]
        pred_set["alt_rc"] = input_set["rc_alt"]
    pred_set["line_id"] = np.array(process_ids).astype(str)
    pred_set["vcf_records"] = vcf_records
    return pred_set


def predict_snvs(model,
                 dataloader,
                 vcf_fpath,
                 batch_size,
                 num_workers=0,
                 dataloader_args=None,
                 vcf_to_region=None,
                 vcf_id_generator_fn=default_vcf_id_gen,
                 evaluation_function=analyse_model_preds,
                 evaluation_function_kwargs={'diff_types': {'logit': Logit()}},
                 sync_pred_writer=None,
                 use_dataloader_example_data=False,
                 return_predictions=False,
                 generated_seq_writer=None
                 ):
    """Predict the effect of SNVs

            Prediction of effects of SNV based on a VCF. If desired the VCF can be stored with the predicted values as
            annotation. For a detailed description of the requirements in the yaml files please take a look at
            kipoi/nbs/variant_effect_prediction.ipynb.

            # Arguments
                model: A kipoi model handle generated by e.g.: kipoi.get_model()
                dataloader: Dataloader factory generated by e.g.: kipoi.get_dataloader_factory()
                vcf_fpath: Path of the VCF defining the positions that shall be assessed. Only SNVs will be tested.
                batch_size: Prediction batch size used for calling the data loader. Each batch will be generated in 4
                    mutated states yielding a system RAM consumption of >= 4x batch size.
                num_workers: Number of parallel workers for loading the dataset.
                dataloader_args: arguments passed on to the dataloader for sequence generation, arguments
                    mentioned in dataloader.yaml > postprocessing > variant_effects > bed_input will be overwritten
                    by the methods here.
                vcf_to_region: Callable that generates a region compatible with dataloader/model from a cyvcf2 record
                vcf_id_generator_fn: Callable that generates a unique ID from a cyvcf2 record
                evaluation_function: effect evaluation function. Default is `analyse_model_preds`, which will get
                    arguments defined in `evaluation_function_kwargs`
                evaluation_function_kwargs: kwargs passed on to `evaluation_function`.
                sync_pred_writer: Single writer or list of writer objects like instances of `VcfWriter`. This object
                    will be called after effect prediction of a batch is done.
                use_dataloader_example_data: Fill out the missing dataloader arguments with the example values given in the
                    dataloader.yaml.
                return_predictions: Return all variant effect predictions as a dictionary. Setting this to False will
                    help maintain a low memory profile and is faster as it avoids concatenating batches after prediction.
                generated_seq_writer: Single writer or list of writer objects like instances of `SyncHdf5SeqWriter`.
                    This object will be called after the DNA sequence sets have been generated. If this parameter is
                    not None, no prediction will be performed and only DNA sequence will be written!! This is relevant
                    if you want to use the `predict_snvs` to generate appropriate input DNA sequences for your model.

            # Returns
                If return_predictions: Dictionary which contains a pandas DataFrame containing the calculated values
                    for each model output (target) column VCF SNV line. If return_predictions == False, returns None.
            """
    import cyvcf2
    model_info_extractor = ModelInfoExtractor(model_obj=model, dataloader_obj=dataloader)

    # If then where do I have to put my bed file in the command?

    exec_files_bed_keys = model_info_extractor.get_exec_files_bed_keys()
    temp_bed3_file = None

    vcf_search_regions = True

    # If there is a field for putting the a postprocessing bed file, then generate the bed file.
    if exec_files_bed_keys is not None:
        if vcf_to_region is not None:
            vcf_search_regions = False

            temp_bed3_file = tempfile.mktemp()  # file path of the temp file

            vcf_fh = cyvcf2.VCF(vcf_fpath, "r")

            with BedWriter(temp_bed3_file) as ofh:
                for record in vcf_fh:
                    if not is_indel_wrapper(record):
                        region = vcf_to_region(record)
                        id = vcf_id_generator_fn(record)
                        for chrom, start, end in zip(region["chrom"], region["start"], region["end"]):
                            ofh.append_interval(chrom=chrom, start=start, end=end, id=id)

            vcf_fh.close()
    else:
        if vcf_to_region is not None:
            logger.warn("`vcf_to_region` will be ignored as it was set, but the dataloader does not define "
                        "a bed_input in dataloader.yaml: "
                        "postprocessing > variant_effects > bed_input.")
    # Assemble the paths for executing the dataloader
    if dataloader_args is None:
        dataloader_args = {}

    # Copy the missing arguments from the example arguments.
    if use_dataloader_example_data:
        for k in dataloader.example_kwargs:
            if k not in dataloader_args:
                dataloader_args[k] = dataloader.example_kwargs[k]

    # If there was a field for dumping the region definition bed file, then use it.
    if (exec_files_bed_keys is not None) and (not vcf_search_regions):
        for k in exec_files_bed_keys:
            dataloader_args[k] = temp_bed3_file

    model_out_annotation = model_info_extractor.get_model_out_annotation()

    out_reshaper = OutputReshaper(model.schema.targets)

    res = []

    it = dataloader(**dataloader_args).batch_iter(batch_size=batch_size,
                                                  num_workers=num_workers)

    # organise the writers in a list
    if sync_pred_writer is not None:
        if not isinstance(sync_pred_writer, list):
            sync_pred_writer = [sync_pred_writer]

    # organise the prediction writers
    if generated_seq_writer is not None:
        if not isinstance(generated_seq_writer, list):
            generated_seq_writer = [generated_seq_writer]

    # Open vcf again
    vcf_fh = cyvcf2.VCF(vcf_fpath, "r")

    # pre-process regions
    keys = set()  # what is that?

    sample_counter = SampleCounter()

    # open the writers if possible:
    if sync_pred_writer is not None:
        [el.open() for el in sync_pred_writer if hasattr(el, "open")]

    # open seq writers if possible:
    if generated_seq_writer is not None:
        [el.open() for el in generated_seq_writer if hasattr(el, "open")]

    for i, batch in enumerate(tqdm(it)):
        # For debugging
        # if i >= 10:
        #     break
        # becomes noticable for large vcf's. Is there a way to avoid it? (i.e. to exploit the iterative nature of dataloading)
        seq_to_mut = model_info_extractor.seq_input_mutator
        seq_to_meta = model_info_extractor.seq_input_metadata
        eval_kwargs = _generate_seq_sets(dataloader.output_schema, batch, vcf_fh, vcf_id_generator_fn,
                                         seq_to_mut=seq_to_mut, seq_to_meta=seq_to_meta,
                                         sample_counter=sample_counter, vcf_search_regions=vcf_search_regions,
                                         generate_rc=model_info_extractor.use_seq_only_rc)
        if eval_kwargs is None:
            # No generated datapoint overlapped any VCF region
            continue

        if generated_seq_writer is not None:
            for writer in generated_seq_writer:
                writer(eval_kwargs)
            # Assume that we don't actually want the predictions to be calculated...
            continue

        if evaluation_function_kwargs is not None:
            assert isinstance(evaluation_function_kwargs, dict)
            for k in evaluation_function_kwargs:
                eval_kwargs[k] = evaluation_function_kwargs[k]

        eval_kwargs["out_annotation_all_outputs"] = model_out_annotation

        res_here = evaluation_function(model, output_reshaper=out_reshaper, **eval_kwargs)
        for k in res_here:
            keys.add(k)
            res_here[k].index = eval_kwargs["line_id"]
        # write the predictions synchronously
        if sync_pred_writer is not None:
            for writer in sync_pred_writer:
                writer(res_here, eval_kwargs["vcf_records"], eval_kwargs["line_id"])
        if return_predictions:
            res.append(res_here)

    vcf_fh.close()

    # open the writers if possible:
    if sync_pred_writer is not None:
        [el.close() for el in sync_pred_writer if hasattr(el, "close")]

    # open seq writers if possible:
    if generated_seq_writer is not None:
        [el.close() for el in generated_seq_writer if hasattr(el, "close")]

    try:
        if temp_bed3_file is not None:
            os.unlink(temp_bed3_file)
    except:
        pass

    if return_predictions:
        res_concatenated = {}
        for k in keys:
            res_concatenated[k] = pd.concat([batch[k]
                                             for batch in res
                                             if k in batch])
        return res_concatenated

    return None


def _get_vcf_to_region(model_info, restriction_bed, seq_length):
    import kipoi
    import pybedtools
    # Select the appropriate region generator
    if restriction_bed is not None:
        # Select the restricted SNV-centered region generator
        pbd = pybedtools.BedTool(restriction_bed)
        vcf_to_region = kipoi_veff.SnvPosRestrictedRg(model_info, pbd)
        logger.info('Restriction bed file defined. Only variants in defined regions will be tested.'
                    'Only defined regions will be tested.')
    elif model_info.requires_region_definition:
        # Select the SNV-centered region generator
        vcf_to_region = kipoi_veff.SnvCenteredRg(model_info, seq_length=seq_length)
        logger.info('Using variant-centered sequence generation.')
    else:
        # No regions can be defined for the given model, VCF overlap will be inferred, hence tabixed VCF is necessary
        vcf_to_region = None
        logger.info('Dataloader does not accept definition of a regions bed-file. Only VCF-variants that lie within'
                    'produced regions can be predicted')
    return vcf_to_region


def score_variants(model,
                   dl_args,
                   input_vcf,
                   output_vcf,
                   scores=["logit_ref", "logit_alt", "ref", "alt", "logit", "diff"],
                   score_kwargs=None,
                   num_workers=0,
                   batch_size=32,
                   source='kipoi',
                   seq_length=None,
                   std_var_id=False,
                   restriction_bed=None,
                   return_predictions=False,
                   output_filter = None):
    """Score variants: annotate the vcf file using
    model predictions for the refernece and alternative alleles
    Args:
      model: model string or a model class instance
      dl_args: dataloader arguments as a dictionary
      input_vcf: input vcf file path
      output_vcf: output vcf file path
      scores: list of score names to compute. See kipoi_veff.scores
      score_kwargs: optional, list of kwargs that corresponds to the entries in score. For details see 
      num_workers: number of paralell workers to use for dataloading
      batch_size: batch_size for dataloading
      source: model source name
      std_var_id: If true then variant IDs in the annotated VCF will be replaced with a standardised, unique ID.
      seq_length: If model accepts variable input sequence length then this value has to be set!
      restriction_bed: If dataloader can be run with regions generated from the VCF then only variants that overlap
      regions defined in `restriction_bed` will be tested.
      return_predictions: return generated predictions also as pandas dataframe.
      output_filter: If set then either a boolean filter or a named filter for model outputs that are reported.
    """
    # TODO - call this function in kipoi_veff.cli.cli_score_variants
    # TODO: Add tests
    import kipoi
    in_vcf_path_abs = os.path.realpath(input_vcf)
    out_vcf_path_abs = os.path.realpath(output_vcf)
    if isinstance(model, str):
        model = kipoi.get_model(model, source=source, with_dataloader=True)
    Dataloader = model.default_dataloader
    vcf_path_tbx = ensure_tabixed_vcf(in_vcf_path_abs)  # TODO - run this within the function
    writer = VcfWriter(model, in_vcf_path_abs, out_vcf_path_abs, standardise_var_id=std_var_id)
    dts = get_scoring_fns(model, scores, score_kwargs)

    # Load effect prediction related model info
    model_info = kipoi_veff.ModelInfoExtractor(model, Dataloader)
    vcf_to_region = _get_vcf_to_region(model_info, restriction_bed, seq_length)

    return predict_snvs(model,
                        Dataloader,
                        vcf_path_tbx,
                        batch_size=batch_size,
                        dataloader_args=dl_args,
                        num_workers=num_workers,
                        vcf_to_region=vcf_to_region,
                        evaluation_function_kwargs={'diff_types': dts, 'output_filter': output_filter},
                        sync_pred_writer=writer,
                        return_predictions=return_predictions)
