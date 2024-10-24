import os
import pathlib

import icon_registration.itk_wrapper as itk_wrapper
import itk
import numpy as np
import vtk
from unigradicon import preprocess, get_unigradicon
from vtk import vtkPointLocator
from vtk.util.numpy_support import numpy_to_vtk

import mesh_processing as mp
from analysis_object import AnalysisObject
from cartilage_shape_processing import thickness_3d_to_2d
from thickness_computation import compute_thickness

DATA_DIR = pathlib.Path(__file__).parent / "data"


def write_vtk_mesh(mesh, filename):
    writer = vtk.vtkPolyDataWriter()
    writer.SetFileName(filename)
    writer.SetInputData(mesh)
    writer.SetFileVersion(42)  # ITK does not support newer version (5.1)
    writer.SetFileTypeToBinary()  # reading and writing binary files is faster
    writer.Write()


def transform_mesh(mesh, transform, filename_prefix, keep_intermediate_outputs):
    """
    Transform the input mesh using the provided transform.

    :param mesh: input mesh
    :param transform: The modelling transform to use (the inverse of image resampling transform)
    :param filename_prefix: prefix (including path) for the intermediate file names
    :return: transformed mesh
    """
    itk_mesh = mp.get_itk_mesh(mesh)
    t_mesh = itk.transform_mesh_filter(itk_mesh, transform=transform)
    # itk.meshwrite(t_mesh, filename_prefix + "_transformed.vtk", binary=True)  # does not work in 5.4 and earlier
    itk.meshwrite(t_mesh, filename_prefix + "_transformed.vtk", compression=True)

    transformed_mesh = mp.read_vtk_mesh(filename_prefix + "_transformed.vtk")
    transformed_mesh.GetPointData().AddArray(mesh.GetPointData().GetArray(0))  # transfer thickness

    if keep_intermediate_outputs:
        write_vtk_mesh(mesh, filename_prefix + "_original.vtk")
    else:
        os.remove(filename_prefix + "_transformed.vtk")

    return transformed_mesh


def into_canonical_orientation(image):
    """
    Reorient the given image into the canonical orientation.

    :param image: input image
    :return: reoriented image
    """
    dicom_lps = itk.SpatialOrientationEnums.ValidCoordinateOrientations_ITK_COORDINATE_ORIENTATION_RAI
    dicom_ras = itk.SpatialOrientationEnums.ValidCoordinateOrientations_ITK_COORDINATE_ORIENTATION_LPI
    dicom_pir = itk.SpatialOrientationEnums.ValidCoordinateOrientations_ITK_COORDINATE_ORIENTATION_ASL
    oriented_image = itk.orient_image_filter(
        image,
        use_image_direction=True,
        # given_coordinate_orientation=dicom_lps,
        # desired_coordinate_orientation=dicom_ras,
        desired_coordinate_orientation=dicom_pir,  # atlas' orientation
    )
    return oriented_image


def sample_distance_from_image(thickness_image, mesh):
    num_points = mesh.GetNumberOfPoints()
    thickness = np.zeros(num_points, dtype=np.float32)
    for i in range(num_points):
        point = mesh.GetPoint(i)
        image_index = thickness_image.TransformPhysicalPointToIndex(point)
        thickness[i] = thickness_image.GetPixel(image_index)

    vtk_array = numpy_to_vtk(thickness, deep=True, array_type=vtk.VTK_FLOAT)
    vtk_array.SetName("vertex_thickness")
    mesh.GetPointData().AddArray(vtk_array)
    return mesh


def analysis_pipeline(input_path, output_path, keep_intermediate_outputs):
    """
    Computes cartilage thickness for femur and tibia from knee MRI.

    :param input_path: path to the input image file, or path to input directory containing DICOM image series.
    :param output_path: path to the desired directory for outputs.
    """
    in_image = itk.imread(input_path, pixel_type=itk.F)
    in_image = into_canonical_orientation(in_image)  # simplifies mesh processing
    in_image = preprocess(in_image, modality="mri")
    os.makedirs(output_path, exist_ok=True)  # also holds intermediate results
    if keep_intermediate_outputs:
        itk.imwrite(in_image, os.path.join(output_path, "in_image.nrrd"))

    print("Segmenting the cartilage")
    obj = AnalysisObject()
    FC_prob, TC_prob = obj.segment(in_image)
    if keep_intermediate_outputs:
        itk.imwrite(FC_prob, os.path.join(output_path, "FC_prob.nrrd"))
        itk.imwrite(TC_prob, os.path.join(output_path, "TC_prob.nrrd"))

    fc_thickness_image, fc_distance, fc_mask = compute_thickness(FC_prob)
    tc_thickness_image, tc_distance, tc_mask = compute_thickness(TC_prob)
    if keep_intermediate_outputs:
        itk.imwrite(fc_thickness_image, os.path.join(output_path, "fc_thickness_image.nrrd"), compression=True)
        itk.imwrite(tc_thickness_image, os.path.join(output_path, "tc_thickness_image.nrrd"), compression=True)
        itk.imwrite(fc_distance, os.path.join(output_path, "fc_distance.nrrd"), compression=True)
        itk.imwrite(tc_distance, os.path.join(output_path, "tc_distance.nrrd"), compression=True)
        itk.imwrite(fc_mask, os.path.join(output_path, "fc_mask-label.nrrd"), compression=True)
        itk.imwrite(tc_mask, os.path.join(output_path, "tc_mask-label.nrrd"), compression=True)

    atlas_filename = DATA_DIR / "atlases/atlas_60_LEFT_baseline_NMI/atlas.nii.gz"
    atlas_image = itk.imread(atlas_filename, itk.F)

    inner_mesh_fc_atlas = mp.read_vtk_mesh(
        DATA_DIR / "atlases/atlas_60_LEFT_baseline_NMI/atlas_FC_inner_mesh_smooth.ply")
    inner_mesh_tc_atlas = mp.read_vtk_mesh(
        DATA_DIR / "atlases/atlas_60_LEFT_baseline_NMI/atlas_TC_inner_mesh_smooth.ply")

    print("Registering the input image to the atlas")
    model = get_unigradicon()
    in_image_D = in_image.astype(itk.D)
    atlas_image_D = atlas_image.astype(itk.D)
    phi_AB, phi_BA = itk_wrapper.register_pair(model, in_image_D, atlas_image_D, finetune_steps=None)
    if keep_intermediate_outputs:
        print("Saving registration results")
        itk.transformwrite(phi_AB, os.path.join(output_path, "resampling.tfm"))
        itk.transformwrite(phi_BA, os.path.join(output_path, "modelling.tfm"))

    print("Transferring the thickness from images to meshes")
    fc_mesh_itk = mp.get_mesh_from_probability_map(FC_prob)
    tc_mesh_itk = mp.get_mesh_from_probability_map(TC_prob)
    fc_mesh = mp.itk_mesh_to_vtk_mesh(fc_mesh_itk)
    tc_mesh = mp.itk_mesh_to_vtk_mesh(tc_mesh_itk)
    fc_mesh = sample_distance_from_image(fc_thickness_image, fc_mesh)
    tc_mesh = sample_distance_from_image(tc_thickness_image, tc_mesh)
    if keep_intermediate_outputs:
        write_vtk_mesh(fc_mesh, output_path + "/fc_mesh_patient.vtk")
        write_vtk_mesh(tc_mesh, output_path + "/tc_mesh_patient.vtk")

    print("Transforming meshes into atlas space")
    fc_mesh_atlas = transform_mesh(fc_mesh, phi_BA, output_path + "/fc_mesh", keep_intermediate_outputs)
    tc_mesh_atlas = transform_mesh(tc_mesh, phi_BA, output_path + "/tc_mesh", keep_intermediate_outputs)
    if keep_intermediate_outputs:
        write_vtk_mesh(fc_mesh_atlas, output_path + "/fc_mesh_atlas.vtk")
        write_vtk_mesh(tc_mesh_atlas, output_path + "/tc_mesh_atlas.vtk")

    print("Mapping thickness from patient meshes to the atlas")
    mapped_mesh_fc = mp.map_attributes(fc_mesh_atlas, inner_mesh_fc_atlas)
    mapped_mesh_tc = mp.map_attributes(tc_mesh_atlas, inner_mesh_tc_atlas)
    if keep_intermediate_outputs:
        write_vtk_mesh(mapped_mesh_fc, output_path + "/mapped_mesh_FC.vtk")
        write_vtk_mesh(mapped_mesh_tc, output_path + "/mapped_mesh_TC.vtk")

    print("Projecting thickness to 2D")
    thickness_3d_to_2d(mapped_mesh_fc, mesh_type='FC', output_filename=output_path + '/thickness_FC')
    thickness_3d_to_2d(mapped_mesh_tc, mesh_type='TC', output_filename=output_path + '/thickness_TC')


if __name__ == "__main__":
    test_cases = [
        r"M:\Dev\Osteoarthritis\OAI_analysis_2\oai_analysis\data\test_data\colab_case\image_preprocessed.nii.gz",
        r"M:\Dev\Osteoarthritis\NAS\OAIBaselineImages\results\0.C.2\9000798\20040924\10249506",
        r"M:\Dev\Osteoarthritis\NAS\OAIBaselineImages\results\0.C.2\9000798\20040924\10249512",
        r"M:\Dev\Osteoarthritis\NAS\OAIBaselineImages\results\0.C.2\9000798\20040924\10249516",
        # r"M:\Dev\Osteoarthritis\NAS\OAIBaselineImages\results\0.C.2\9000798\20040924\10249517",  # parsed incorrectly
    ]

    for i, case in enumerate(test_cases):
        print(f"\nProcessing case {case}")
        output_path = f"./OAI_results/case_{i:03d}"
        analysis_pipeline(case, output_path, keep_intermediate_outputs=True)
