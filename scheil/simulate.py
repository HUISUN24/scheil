import sys
import numpy as np
from collections import defaultdict
from pycalphad import equilibrium, variables as v
from pycalphad.codegen.callables import build_phase_records
from pycalphad.core.calculate import _sample_phase_constitution
from pycalphad.core.utils import instantiate_models, unpack_components, filter_phases, point_sample
from .solidification_result import SolidificationResult
from .utils import local_sample, get_phase_amounts
from .ordering import create_ordering_records, rename_disordered_phases


def is_converged(eq):
    """
    Return true if there are phase fractions that are non-NaN

    Parameters
    ----------
    eq : pycalphad.LightDataset

    """
    if np.any(~np.isnan(eq.NP)):
        return True
    return False


def _update_points(eq, points_dict, dof_dict, local_pdens=0, verbose=False):
    """
    Update the points_dict by appending new points.

    Parameters
    ----------
    eq : pycalphad.LightDataset
        Point equilibrium result. Incompatible with xarray.Dataset objects.
    points_dict : dict[str, np.ndarray]
        Map of phase name to array of points
    dof_dict : dict[str, list[int]]
        Map of phase name to the sublattice degrees of freedom.
    local_pdens : Optional[int]
        Point density for local sampling. If zero (the default) only the equilibrium site fractions will be added.
    verbose : Optional[bool]

    """
    # Update the points dictionary with local samples around the equilibrium site fractions
    for vtx in eq.vertex.squeeze():
        ph = str(eq.Phase.squeeze()[vtx])
        pts = points_dict.get(ph)
        if pts is not None:
            if verbose:
                print(f'Adding points to {ph}. ', end='')
            dof = dof_dict[ph]
            eq_pts = eq.Y.squeeze()[vtx, :sum(dof)].reshape(1, -1)
            if local_pdens > 0:
                points_dict[ph] = np.concatenate([pts, local_sample(eq_pts, dof, pdens=local_pdens)], axis=0)
            else:
                points_dict[ph] = np.concatenate([pts, eq_pts], axis=0)


def simulate_scheil_solidification(dbf, comps, phases, composition,
                                   start_temperature, step_temperature=1.0,
                                   liquid_phase_name='LIQUID', eq_kwargs=None,
                                   stop=0.0001, verbose=False, adaptive=True):
    """Perform a Scheil-Gulliver solidification simulation.

    Parameters
    ----------
    dbf : pycalphad.Database
        Database object.
    comps : list
        List of components in the system.
    phases : list
        List of phases in the system.
    composition : Dict[v.X, float]
        Dictionary of independent `v.X` composition variables.
    start_temperature : float
        Starting temperature for simulation. Must be single phase liquid.
    step_temperature : Optional[float]
        Temperature step size. Defaults to 1.0.
    liquid_phase_name : Optional[str]
        Name of the phase treated as liquid (i.e. the phase with infinitely
        fast diffusion). Defaults to 'LIQUID'.
    eq_kwargs: Optional[Dict[str, Any]]
        Keyword arguments for equilibrium
    stop: Optional[float]
        Stop when the phase fraction of liquid is below this amount.
    adaptive: Optional[bool]
        Whether to add additional points near the equilibrium points at each
        step. Only takes effect if ``points`` is in the eq_kwargs dict.

    Returns
    -------
    SolidificationResult

    """
    eq_kwargs = eq_kwargs or dict()
    STEP_SCALE_FACTOR = 1.2  # How much to try to adapt the temperature step by
    MAXIMUM_STEP_SIZE_REDUCTION = 5.0
    T_STEP_ORIG = step_temperature
    phases = filter_phases(dbf, unpack_components(dbf, comps), phases)
    ordering_records = create_ordering_records(dbf, comps, phases)
    models = instantiate_models(dbf, comps, phases)
    if verbose:
        print('building PhaseRecord objects... ', end='')
    phase_records = build_phase_records(dbf, comps, phases, [v.N, v.P, v.T], models)
    if verbose:
        print('done')
    filtered_disordered_phases = {ord_rec.disordered_phase_name for ord_rec in ordering_records}
    solid_phases = sorted((set(phases) | filtered_disordered_phases) - {liquid_phase_name})
    temp = start_temperature
    independent_comps = sorted([str(comp)[2:] for comp in composition.keys()])
    
    x_liquid = {comp: [composition[v.X(comp)]] for comp in independent_comps}
    fraction_solid = [0.0]
    temperatures = [temp]
    phase_amounts = {ph: [0.0] for ph in solid_phases}
    x_phases = defaultdict(dict)
    Y_phases = defaultdict(dict)
    for ph in solid_phases:
        Y_phases[ph] = [np.nan]
        for comp in independent_comps:
            x_phases[ph][comp] = [np.nan]

    if adaptive:
        dof_dict = {phase_name: list(map(len, mod.constituents)) for phase_name, mod in models.items()}
        eq_kwargs.setdefault('calc_opts', {})
        # TODO: handle per-phase/unpackable points and pdens
        if 'points' not in eq_kwargs['calc_opts']:
            if verbose:
                print('generating points... ', end='')
            points_dict = {}
            for phase_name, mod in models.items():
                if verbose:
                    print(phase_name, end=' ')
                pdens = eq_kwargs['calc_opts'].get('pdens', 50)
                points_dict[phase_name] = _sample_phase_constitution(mod, point_sample, True, pdens=pdens)
            eq_kwargs['calc_opts']['points'] = points_dict
            if verbose:
                print('done')

    converged = False
    phases_seen = {liquid_phase_name, ''}
    liquid_comp = composition
    while fraction_solid[-1] < 1:
        conds = {v.T: temp, v.P: 101325.0, v.N: 1.0}
        comp_conds = liquid_comp
        fmt_comp_conds = ', '.join([f'{c}={val:0.2f}' for c, val in comp_conds.items()])
        conds.update(comp_conds)
        eq = equilibrium(dbf, comps, phases, conds, model=models, phase_records=phase_records, to_xarray=False, **eq_kwargs)
        if adaptive:
            _update_points(eq, eq_kwargs['calc_opts']['points'], dof_dict, verbose=verbose)
        eq = eq.get_dataset()  # convert LightDataset to Dataset for fancy indexing
        eq = rename_disordered_phases(eq, ordering_records)
        eq_phases = eq.Phase.values.squeeze().tolist()
        new_phases_seen = set(eq_phases).difference(phases_seen)
        if len(new_phases_seen) > 0:
            if verbose:
                print(f'New phases seen: {new_phases_seen}. ', end='')
            phases_seen |= new_phases_seen
        if liquid_phase_name not in eq["Phase"].values.squeeze():
            found_ph = set(eq_phases) - {''}
            if verbose:
                print(f'No liquid phase found at T={temp:0.3f}, {fmt_comp_conds}. (Found {found_ph}) ', end='')
            if len(found_ph) == 0:
                # No phases found in equilibrium. Just continue on lowering the temperature without changing anything
                if verbose:
                    print(f'(Convergence failure) ', end='')
            if T_STEP_ORIG / step_temperature > MAXIMUM_STEP_SIZE_REDUCTION:
                # Only found solid phases and the step size has already been reduced. Stop running without converging.
                if verbose:
                    print('Maximum step size reduction exceeded. Stopping.')
                converged = False
                break
            else:
                # Only found solid phases. Try reducing the step size to zero-in on the correct phases
                if verbose:
                    print(f'Stepping back and reducing step size.')
                temp += step_temperature
                step_temperature /= STEP_SCALE_FACTOR
                temp -= step_temperature
                continue
        # TODO: Will break if there is a liquid miscibility gap
        liquid_vertex = sorted(np.nonzero(eq["Phase"].values.squeeze().flat == liquid_phase_name))[0]
        liquid_comp = {}
        for comp in independent_comps:
            x = float(eq["X"].isel(vertex=liquid_vertex).squeeze().sel(component=comp).values)
            x_liquid[comp].append(x)
            liquid_comp[v.X(comp)] = x
        np_liq = np.nansum(eq.where(eq["Phase"] == liquid_phase_name).NP.values)
        current_fraction_solid = float(fraction_solid[-1])
        found_phase_amounts = [(liquid_phase_name, np_liq)]  # tuples of phase name, amount
        for solid_phase in solid_phases:
            if solid_phase not in eq_phases:
                phase_amounts[solid_phase].append(0.0)
                Y_phases[solid_phase].append(np.nan)
                for comp in independent_comps:
                    x_phases[solid_phase][comp].append(np.nan)
                continue
            np_tieline = np.nansum(eq.isel(vertex=eq_phases.index(solid_phase))["NP"].values.squeeze())
            found_phase_amounts.append((solid_phase, np_tieline))
            delta_fraction_solid = (1 - current_fraction_solid) * np_tieline
            current_fraction_solid += delta_fraction_solid
            phase_amounts[solid_phase].append(delta_fraction_solid)
            vertex = sorted(np.nonzero(eq["Phase"].values.squeeze().flat == solid_phase))[0]
            Y_phases[solid_phase].append(list(eq["Y"].isel(vertex=vertex).squeeze().values))
            for comp in independent_comps:
                if np.sum(eq["X"].isel(vertex=vertex).squeeze().sel(component=comp).values)==0:
                    x_phases[solid_phase][comp].append(np.nan)
                else:
                    x_phases[solid_phase][comp].append(float(eq["X"].isel(vertex=vertex).squeeze().sel(component=comp).values))
        fraction_solid.append(current_fraction_solid)
        temperatures.append(temp)
        NL = 1 - fraction_solid[-1]
        if verbose:
            phase_amnts = ' '.join([f'NP({ph})={amnt:0.3f}' for ph, amnt in found_phase_amounts])
            if NL < 1.0e-3:
                print(f'T={temp:0.3f}, {fmt_comp_conds}, ΔT={step_temperature:0.3f}, NL: {NL:.2E}, {phase_amnts} ', end='')
            else:
                print(f'T={temp:0.3f}, {fmt_comp_conds}, ΔT={step_temperature:0.3f}, NL: {NL:0.3f}, {phase_amnts} ', end='')
        if NL < stop:
            if verbose:
                print(f'Liquid fraction below criterion {stop} . Stopping at {fmt_comp_conds}')
            converged = True
            break
        if verbose:
            print()  # add line break
        temp -= step_temperature

    if fraction_solid[-1] < 1:
        for comp in independent_comps:
            x_liquid[comp].append(np.nan)
        fraction_solid.append(1.0)
        temperatures.append(temp)
        # set the final phase amount to the phase fractions in the eutectic
        # this method gives the sum total phase amounts of 1.0 by construction
        for solid_phase in solid_phases:
            if solid_phase in eq_phases:
                amount = np.nansum(eq.isel(vertex=eq_phases.index(solid_phase))["NP"].values.squeeze())
                try:
                    phase_amounts[solid_phase].append(float(amount) * (1 - current_fraction_solid))
                except UnboundLocalError:
                    phases_solid = set(eq_phases) - {''}
                    raise ValueError("The input temperature is too low to support a liquid phase, it only contains "+', '.join(phases_solid))
            else:
                phase_amounts[solid_phase].append(0.0)
            vertex = sorted(np.nonzero(eq["Phase"].values.squeeze().flat == solid_phase))[0]
            Y_phases[solid_phase].append(list(eq["Y"].isel(vertex=vertex).squeeze().values))
            for comp in independent_comps:
                if np.sum(eq["X"].isel(vertex=vertex).squeeze().sel(component=comp).values)==0:
                    x_phases[solid_phase][comp].append(np.nan)
                else:
                    x_phases[solid_phase][comp].append(float(eq["X"].isel(vertex=vertex).squeeze().sel(component=comp).values))

    return SolidificationResult(x_liquid, x_phases, Y_phases, fraction_solid, temperatures, phase_amounts, converged, "scheil")


def simulate_equilibrium_solidification(dbf, comps, phases, composition,
                                        start_temperature, step_temperature=1.0,
                                        liquid_phase_name='LIQUID', adaptive=True, eq_kwargs=None,
                                        binary_search_tol=0.1,
                                        verbose=False):
    """
    Compute the equilibrium solidification path.

    Decreases temperature until no liquid is found, performing a binary search to get the soildus temperature.

    dbf : pycalphad.Database
        Database object.
    comps : list
        List of components in the system.
    phases : list
        List of phases in the system.
    composition : Dict[v.X, float]
        Dictionary of independent `v.X` composition variables.
    start_temperature : float
        Starting temperature for simulation. Should be single phase liquid.
    step_temperature : Optional[float]
        Temperature step size. Defaults to 1.0.
    liquid_phase_name : Optional[str]
        Name of the phase treated as liquid (i.e. the phase with infinitely
        fast diffusion). Defaults to 'LIQUID'.
    eq_kwargs: Optional[Dict[str, Any]]
        Keyword arguments for equilibrium
    binary_search_tol : float
        Stop the binary search when the difference between temperatures is less than this amount.
    adaptive: Optional[bool]
        Whether to add additional points near the equilibrium points at each
        step. Only takes effect if ``points`` is in the eq_kwargs dict.

    """
    eq_kwargs = eq_kwargs or dict()
    phases = filter_phases(dbf, unpack_components(dbf, comps), phases)
    ordering_records = create_ordering_records(dbf, comps, phases)
    filtered_disordered_phases = {ord_rec.disordered_phase_name for ord_rec in ordering_records}
    solid_phases = sorted((set(phases) | filtered_disordered_phases) - {liquid_phase_name})
    independent_comps = sorted([str(comp)[2:] for comp in composition.keys()])
    models = instantiate_models(dbf, comps, phases)
    if verbose:
        print('building PhaseRecord objects... ', end='')
    phase_records = build_phase_records(dbf, comps, phases, [v.N, v.P, v.T], models)
    if verbose:
        print('done')
    conds = {v.P: 101325, v.N: 1.0}
    conds.update(composition)


    if adaptive:
        dof_dict = {phase_name: list(map(len, mod.constituents)) for phase_name, mod in models.items()}
        eq_kwargs.setdefault('calc_opts', {})
        # TODO: handle per-phase/unpackable points and pdens
        if 'points' not in eq_kwargs['calc_opts']:
            # construct a points dict for the user
            points_dict = {}
            for phase_name, mod in models.items():
                pdens = eq_kwargs['calc_opts'].get('pdens', 50)
                points_dict[phase_name] = _sample_phase_constitution(mod, point_sample, True, pdens=pdens)
            eq_kwargs['calc_opts']['points'] = points_dict

    temperatures = []
    x_liquid = {comp: [] for comp in independent_comps}
    fraction_solid = []
    phase_amounts = {ph: [] for ph in solid_phases}  # instantaneous phase amounts
    cum_phase_amounts = {ph: [] for ph in solid_phases}
    x_phases = defaultdict(dict)
    Y_phases = defaultdict(dict)
    for ph in solid_phases:
        Y_phases[ph] = []
        for comp in independent_comps:
            x_phases[ph][comp] = []
    converged = False
    current_T = start_temperature
    if verbose:
        print('T=')
    while (fraction_solid[-1] < 1 if len(fraction_solid) > 0 else True) and not converged:
        sys.stdout.flush()
        conds[v.T] = current_T
        if verbose:
            print(f'{current_T} ', end='')
        eq = equilibrium(dbf, comps, phases, conds, model=models, phase_records=phase_records, to_xarray=False, **eq_kwargs)
        if not is_converged(eq):
            if verbose:
                comp_conds = {cond: val for cond, val in conds.items() if isinstance(cond, v.X)}
                print(f"Convergence failure at T={conds[v.T]} X={comp_conds} ")
        if adaptive:
            # Update the points dictionary with local samples around the equilibrium site fractions
            _update_points(eq, eq_kwargs['calc_opts']['points'], dof_dict)
        if liquid_phase_name in eq.Phase:
            # Add the liquid phase composition
            # TODO: will break in a liquid miscibility gap
            liquid_vertex = np.nonzero(eq.Phase == liquid_phase_name)[-1][0]
            for comp in independent_comps:
                x_liquid[comp].append(float(eq.X[..., liquid_vertex, eq.component.index(comp)]))
            temperatures.append(current_T)
            current_T -= step_temperature
        else:
            # binary search to find the solidus
            T_high = current_T + step_temperature  # High temperature, liquid
            T_low = current_T  # Low temperature, solids only
            found_ph = set(eq.Phase[eq.Phase != ''].tolist())
            if verbose:
                print(f'Found phases {found_ph}. Starting binary search between T={(T_low, T_high)} ', end='')
            while (T_high - T_low) > binary_search_tol:
                bin_search_T = (T_high - T_low) * 0.5 + T_low
                conds[v.T] = bin_search_T
                eq = equilibrium(dbf, comps, phases, conds, model=models, phase_records=phase_records, to_xarray=False, **eq_kwargs)
                if adaptive:
                    # Update the points dictionary with local samples around the equilibrium site fractions
                    _update_points(eq, eq_kwargs['calc_opts']['points'], dof_dict)
                if not is_converged(eq):
                    if verbose:
                        comp_conds = {cond: val for cond, val in conds.items() if isinstance(cond, v.X)}
                        print(f"Convergence failure at T={conds[v.T]} X={comp_conds} ")
                if liquid_phase_name in eq.Phase:
                    T_high = bin_search_T
                else:
                    T_low = bin_search_T
            converged = True
            conds[v.T] = T_low
            temperatures.append(T_low)
            eq = equilibrium(dbf, comps, phases, conds, model=models, phase_records=phase_records, to_xarray=False, **eq_kwargs)
            if not is_converged(eq):
                if verbose:
                    comp_conds = {cond: val for cond, val in conds.items() if isinstance(cond, v.X)}
                    print(f"Convergence failure at T={conds[v.T]} X={comp_conds} ")
            if verbose:
                found_phases = set(eq.Phase[eq.Phase != ''].tolist())
                print(f"Finshed binary search at T={conds[v.T]} with phases={found_phases} and NP={eq.NP.squeeze()[:len(found_phases)]}")
            if adaptive:
                # Update the points dictionary with local samples around the equilibrium site fractions
                _update_points(eq, eq_kwargs['calc_opts']['points'], dof_dict)
            # Set the liquid phase composition to NaN
            for comp in independent_comps:
                x_liquid[comp].append(float(np.nan))

        # Calculate fraction of solid and solid phase amounts
        current_fraction_solid = 0.0
        eq = rename_disordered_phases(eq.get_dataset(), ordering_records)
        current_cum_phase_amnts = get_phase_amounts(eq.Phase.values.squeeze(), eq.NP.squeeze(), solid_phases)
        for solid_phase, amount in current_cum_phase_amnts.items():
            # Since the equilibrium calculations always give the "cumulative" phase amount,
            # we need to take the difference to get the instantaneous.
            cum_phase_amounts[solid_phase].append(amount)
            if len(phase_amounts[solid_phase]) == 0:
                phase_amounts[solid_phase].append(amount)
            else:
                phase_amounts[solid_phase].append(amount - cum_phase_amounts[solid_phase][-2])
            vertex = sorted(np.nonzero(eq["Phase"].values.squeeze().flat == solid_phase))[0]
            Y_phases[solid_phase].append(list(eq["Y"].isel(vertex=vertex).squeeze().values))
            for comp in independent_comps:
                if np.sum(eq["X"].isel(vertex=vertex).squeeze().sel(component=comp).values)==0:
                    x_phases[solid_phase][comp].append(np.nan)
                else:
                    x_phases[solid_phase][comp].append(float(eq["X"].isel(vertex=vertex).squeeze().sel(component=comp).values))
            current_fraction_solid += amount
            
        fraction_solid.append(current_fraction_solid)

    converged = True if np.isclose(fraction_solid[-1], 1.0) else False
    return SolidificationResult(x_liquid, x_phases, Y_phases, fraction_solid, temperatures, phase_amounts, converged, "equilibrium")
