import torch
import itertools
from .aev_base import AEVComputer
from . import buildin_const_file
from .torchaev import AEV
from . import _utils


class NeighborAEV(AEVComputer):
    """The AEV computer fully implemented using pytorch, making use of neighbor list"""

    def __init__(self, dtype=torch.cuda.float32, const_file=buildin_const_file):
        super(NeighborAEV, self).__init__(dtype, const_file)

    def radial_subaev(self, center, neighbors):
        """Compute the radial subAEV of the center atom given neighbors

        The radial AEV is define in https://arxiv.org/pdf/1610.08935.pdf equation 3.
        The sum computed by this method is over all given neighbors, so the caller
        of this method need to select neighbors if the caller want a per species subAEV.

        Parameters
        ----------
        center : pytorch tensor of `dtype`
            A tensor of shape (conformations, 3) that stores the xyz coordinate of the
            center atoms.
        neighbors : pytorch tensor of `dtype`
            A tensor of shape (conformations, N, 3) where N is the number of neighbors.
            The tensor stores the xyz coordinate of the neighbor atoms. Note that different
            conformations might have different neighbor atoms within the cutoff radius, if
            this is the case, the union of neighbors of all conformations should be given for
            this parameter.

        Returns
        -------
        pytorch tensor of `dtype`
            A tensor of shape (conformations, `per_species_radial_length()`) storing the subAEVs.
        """
        atoms = neighbors.shape[1]
        Rij_vec = neighbors - center.view(-1, 1, 3)
        """pytorch tensor of shape (conformations, N, 3) storing the Rij vectors where i is the
        center atom, and j is a neighbor. The Rij of conformation n is stored as (n,j,:)"""
        distances = torch.sqrt(torch.sum(Rij_vec ** 2, dim=-1))
        """pytorch tensor of shape (conformations, N) storing the |Rij| length where i is the
        center atom, and j is a neighbor. The |Rij| of conformation n is stored as (n,j)"""

        # use broadcasting semantics to do Cartesian product on constants
        # shape convension (conformations, atoms, EtaR, ShfR)
        distances = distances.view(-1, atoms, 1, 1)
        fc = AEV._cutoff_cosine(distances, self.constants['Rcr'])
        eta = torch.Tensor(self.constants['EtaR']).type(
            self.dtype).view(1, 1, -1, 1)
        radius_shift = torch.Tensor(self.constants['ShfR']).type(
            self.dtype).view(1, 1, 1, -1)
        # Note that in the equation in the paper there is no 0.25 coefficient, but in NeuroChem there is such a coefficient. We choose to be consistent with NeuroChem instead of the paper here.
        ret = 0.25 * torch.exp(-eta * (distances - radius_shift)**2) * fc
        # end of shape convension
        ret = torch.sum(ret, dim=1)
        # flat the last two dimensions to view the subAEV as one dimensional vector
        return ret.view(-1, self.per_species_radial_length())

    def angular_subaev(self, center, neighbors):
        """Compute the angular subAEV of the center atom given neighbor pairs.

        The angular AEV is define in https://arxiv.org/pdf/1610.08935.pdf equation 4.
        The sum computed by this method is over all given neighbor pairs, so the caller
        of this method need to select neighbors if the caller want a per species subAEV.

        Parameters
        ----------
        center : pytorch tensor of `dtype`
            A tensor of shape (conformations, 3) that stores the xyz coordinate of the
            center atoms.
        neighbors : pytorch tensor of `dtype`
            A tensor of shape (conformations, N, 2, 3) where N is the number of neighbor pairs.
            The tensor stores the xyz coordinate of the 2 atoms in neighbor pairs. Note that
            different conformations might have different neighbor pairs within the cutoff radius,
            if this is the case, the union of neighbors of all conformations should be given for
            this parameter.

        Returns
        -------
        pytorch tensor of `dtype`
            A tensor of shape (conformations, `per_species_angular_length()`) storing the subAEVs.
        """
        pairs = neighbors.shape[1]
        Rij_vec = neighbors - center.view(-1, 1, 1, 3)
        """pytorch tensor of shape (conformations, N, 2, 3) storing the Rij vectors where i is the
        center atom, and j is a neighbor. The vector (n,k,l,:) is the Rij where j refer to the l-th
        atom of the k-th pair."""
        R_distances = torch.sqrt(torch.sum(Rij_vec ** 2, dim=-1))
        """pytorch tensor of shape (conformations, N, 2) storing the |Rij| length where i is the
        center atom, and j is a neighbor. The value at (n,k,l) is the |Rij| where j refer to the
        l-th atom of the k-th pair."""

        # Compute the product of two distances |Rij| * |Rik| where j and k are the two atoms in
        # a pair. The result tensor would have shape (conformations, pairs)
        Rijk_distance_prods = R_distances[:, :, 0] * R_distances[:, :, 1]

        # Compute the inner product Rij (dot) Rik where j and k are the two atoms in a pair.
        # The result tensor would have shape (conformations, pairs)
        Rijk_inner_prods = torch.sum(
            Rij_vec[:, :, 0, :] * Rij_vec[:, :, 1, :], dim=-1)

        # Compute the angles jik with i in the center and j and k are the two atoms in a pair.
        # The result tensor would have shape (conformations, pairs)
        # 0.95 is multiplied to the cos values to prevent acos from returning NaN.
        cos_angles = 0.95 * Rijk_inner_prods / Rijk_distance_prods
        angles = torch.acos(cos_angles)

        # use broadcasting semantics to combine constants
        # shape convension (conformations, pairs, EtaA, Zeta, ShfA, ShfZ)
        angles = angles.view(-1, pairs, 1, 1, 1, 1)
        Rij = R_distances.view(-1, pairs, 2, 1, 1, 1, 1)
        fcj = AEV._cutoff_cosine(Rij, self.constants['Rca'])
        eta = torch.Tensor(self.constants['EtaA']).type(
            self.dtype).view(1, 1, -1, 1, 1, 1)
        zeta = torch.Tensor(self.constants['Zeta']).type(
            self.dtype).view(1, 1, 1, -1, 1, 1)
        radius_shifts = torch.Tensor(self.constants['ShfA']).type(
            self.dtype).view(1, 1, 1, 1, -1, 1)
        angle_shifts = torch.Tensor(self.constants['ShfZ']).type(
            self.dtype).view(1, 1, 1, 1, 1, -1)
        ret = 2 * ((1 + torch.cos(angles - angle_shifts)) / 2) ** zeta * \
            torch.exp(-eta * ((Rij[:, :, 0, :, :, :, :] + Rij[:, :, 1, :, :, :, :]) / 2 - radius_shifts)
                      ** 2) * fcj[:, :, 0, :, :, :, :] * fcj[:, :, 1, :, :, :, :]
        # end of shape convension
        ret = torch.sum(ret, dim=1)
        # flat the last 4 dimensions to view the subAEV as one dimension vector
        return ret.view(-1, self.per_species_angular_length())

    def __call__(self, coordinates, species):
        # For the docstring of this method, refer to the base class
        conformations = coordinates.shape[0]
        atoms = coordinates.shape[1]

        R_vecs = coordinates.unsqueeze(1) - coordinates.unsqueeze(2)
        """pytorch tensor of `dtype`: A tensor of shape (conformations, atoms, atoms, 3)
        that stores Rij vectors. The 3 dimensional vector at (N, i, j, :) is the Rij vector
        of conformation N.
        """

        R_distances = torch.sqrt(torch.sum(R_vecs ** 2, dim=-1))
        """pytorch tensor of `dtype`: A tensor of shape (conformations, atoms, atoms)
        that stores |Rij|, i.e. the length of Rij vectors. The value at (N, i, j) is
        the |Rij| of conformation N.
        """

        # Compute the list of atoms inside cutoff radius
        # The list is stored as a tensor of shape (atoms, atoms)
        # where the value at (i,j) == 1 means j is a neighbor of i, otherwise
        # the value should be 0
        in_Rcr = R_distances <= self.constants['Rcr']
        in_Rcr = torch.sum(in_Rcr, dim=0) > 0
        # set diagnoal elements to 0
        in_Rcr = in_Rcr * (1 - torch.eye(atoms, dtype=in_Rcr.dtype))
        in_Rca = R_distances <= self.constants['Rca']
        in_Rca = torch.sum(in_Rca, dim=0) > 0
        # set diagnoal elements to 0
        in_Rca = in_Rca * (1 - torch.eye(atoms, dtype=in_Rca.dtype))

        # Compute species selectors for all supported species.
        # A species selector is a tensor of shape (atoms,)
        # where the value == 1 means the atom with that index is the species
        # of the selector otherwise value should be 0.
        species_selectors = {}
        for i in range(atoms):
            s = species[i]
            if s not in species_selectors:
                species_selectors[s] = torch.zeros(atoms, dtype=in_Rcr.dtype)
            species_selectors[s][i] = 1

        # Compute the list of neighbors of various species
        # The list is stored as a tensor of shape (atoms, atoms)
        # where the value at (i,j) == 1 means j is a neighbor of i and j has the
        # specified species, otherwise the value should be 0
        species_neighbors = {}
        for s in species_selectors:
            selector = species_selectors[s].unsqueeze(0)
            species_neighbors[s] = (in_Rcr * selector, in_Rca * selector)

        # compute radial AEV
        radial_aevs = []
        """The list whose elements are full radial AEV of each atom"""
        for i in range(atoms):
            radial_aev = []
            """The list whose elements are atom i's per species subAEV of each species"""
            for s in self.species:
                is_zero = False
                if s in species_neighbors:
                    indices = species_neighbors[s][0][i, :].nonzero().view(-1)
                    if indices.shape[0] > 0:
                        neighbors = coordinates.index_select(1, indices)
                        radial_aev.append(self.radial_subaev(
                            coordinates[:, i, :], neighbors))
                    else:
                        is_zero = True
                else:
                    is_zero = True
                if is_zero:
                    radial_aev.append(torch.zeros(
                        conformations, self.per_species_radial_length(), dtype=self.dtype))
            radial_aev = torch.cat(radial_aev, dim=1)
            radial_aevs.append(radial_aev)
        radial_aevs = torch.stack(radial_aevs, dim=1)

        # compute angular AEV
        angular_aevs = []
        """The list whose elements are full angular AEV of each atom"""
        for i in range(atoms):
            angular_aev = []
            """The list whose elements are atom i's per species subAEV of each species"""
            for j, k in itertools.combinations_with_replacement(self.species, 2):
                is_zero = False
                if j in species_neighbors and k in species_neighbors:
                    indices_j = species_neighbors[j][1][i, :].nonzero(
                    ).view(-1)
                    neighbors_j = coordinates.index_select(1, indices_j)
                    if j != k and indices_j.shape[0] > 0:
                        indices_k = species_neighbors[k][1][i, :].nonzero(
                        ).view(-1)
                        neighbors_k = coordinates.index_select(1, indices_k)
                        if indices_k.shape[0] > 0:
                            neighbors = _utils.cartesian_prod(
                                neighbors_j, neighbors_k, dim=1, newdim=2)
                        else:
                            is_zero = True
                    elif indices_j.shape[0] > 1:
                        neighbors = _utils.combinations(
                            neighbors_j, 2, dim=1, newdim=2)
                    else:
                        is_zero = True
                    if not is_zero:
                        angular_aev.append(self.angular_subaev(
                            coordinates[:, i, :], neighbors))
                else:
                    is_zero = True
                if is_zero:
                    angular_aev.append(torch.zeros(
                        conformations, self.per_species_angular_length(), dtype=self.dtype))
            angular_aev = torch.cat(angular_aev, dim=1)
            angular_aevs.append(angular_aev)
        angular_aevs = torch.stack(angular_aevs, dim=1)

        return radial_aevs, angular_aevs
